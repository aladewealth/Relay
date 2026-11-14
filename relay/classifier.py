"""Message analysis: ONE SubQ /systemone call carrying every question (classification + extraction picks).
Falls back to a keyword heuristic (source="fallback") when SubQ is unavailable."""
import time

from .extraction import generate_candidates
from .subq import SubQClient, SubQError

CATEGORIES = {
    "billing": "Charges, invoices, payment methods, pricing, double charges",
    "technical_support": "Product or app not working, bugs, errors, setup, connectivity",
    "account_access": "Login, password, 2FA, locked account, suspected account takeover",
    "complaint": "Dissatisfaction with service, staff or experience, without a more specific category",
    "refund": "Customer wants money back or wants to return an item",
    "shipping_delivery": "Order status, tracking, late, lost or damaged deliveries",
    "general_inquiry": "Questions about products, policies, hours, how things work",
    "other": "Spam, unrelated, or does not fit any category",
}
INTENTS = {
    "request_refund": "Wants money back",
    "report_bug": "Reports a defect, error or something broken",
    "cannot_login": "Cannot sign in, password or 2FA problem",
    "cancel_subscription": "Wants to cancel a subscription or account",
    "change_plan": "Wants to upgrade/downgrade/switch plan",
    "track_order": "Wants to know where an order is",
    "report_lost_or_damaged": "Package lost, missing or arrived damaged",
    "dispute_charge": "Disputes or questions a charge, double charge, chargeback threat",
    "return_item": "Wants to return or exchange a product",
    "ask_question": "Asks for information",
    "give_feedback": "Shares feedback, praise or a suggestion",
    "update_details": "Wants to change email, address, payment method or profile details",
    "security_concern": "Suspects hacking, fraud or unauthorised access",
    "complain_service": "Complains about service quality or support experience",
    "other": None,
}
ACTIONS = {
    "issue_refund": "Refund money",
    "replace_or_reship": "Send a replacement or reship",
    "cancel_subscription": "Cancel a subscription",
    "change_plan": "Change subscription plan",
    "reset_access": "Restore account access / reset password or 2FA",
    "investigate_issue": "Investigate or fix a technical problem",
    "provide_information": "Answer a question or give status",
    "update_account": "Update account or profile details",
    "speak_to_manager": "Wants a supervisor or human",
    "no_action": "No action requested",
}
PRIORITY_LEVELS = ["low", "medium", "high", "urgent"]


def _state(text, customer_name, channel, history, prior_customer_msgs):
    hist = "\n".join(f"{m['role']}: {m['text'][:400]}" for m in history[-6:]) or "(none)"
    return (f"Support message received via {channel or 'unknown'} from {customer_name or 'a customer'}.\n"
            f"Earlier customer messages in this conversation still unresolved: {prior_customer_msgs}.\n"
            f"Conversation so far:\n{hist}\n\nNEW CUSTOMER MESSAGE:\n{text}")


def build_questions(candidates):
    """Returns (questions, pick_maps). pick_maps maps option keys back to candidate values."""
    q = {
        "category": {"type": "choice", "instructions": "Which support category best fits the NEW CUSTOMER MESSAGE?",
                     "criteria": CATEGORIES},
        "intent": {"type": "choice", "instructions": "What is the customer's main intent in the NEW CUSTOMER MESSAGE?",
                   "criteria": INTENTS},
        "priority": {"type": "score", "instructions": "How urgently does support need to act? Urgent = money lost right now, security breach, safety risk, legal threat or business blocked; high = customer blocked or very upset; medium = needs action soon; low = informational.",
                     "criteria": PRIORITY_LEVELS},
        "frustrated": {"type": "noul", "instructions": "Is the customer frustrated, angry or upset?",
                       "criteria": {"true": "Annoyed, angry, upset, exasperated, sarcastic or threatening",
                                    "false": "Neutral, calm or positive"}},
        "time_sensitive": {"type": "noul", "instructions": "Is the request time-sensitive (deadline, event, ongoing loss, outage)?",
                           "criteria": {"true": "Has a deadline or gets worse with time", "false": "Can wait without harm"}},
        "requested_action": {"type": "choice", "instructions": "What action is the customer asking support to take?",
                             "criteria": ACTIONS},
        "needs_human": {"type": "noul", "instructions": "Does this case need a human agent rather than an automated draft? Yes for legal threats, safety issues, chargebacks or bank disputes, VIP/business-critical accounts, suspected fraud or account takeover, repeated contact about the same unresolved issue, or complex multi-issue cases.",
                        "criteria": {"true": "Legal, safety, chargeback, fraud, VIP, repeated contact or complex",
                                     "false": "Routine request an agent can approve from a draft"}},
    }
    maps = {}
    refs = candidates["references"]
    if refs:
        opts = {f"c{i}": f"{c['value']} ({c['kind']} reference)" for i, c in enumerate(refs)}
        opts["none"] = "No order/ticket/invoice reference in the message"
        q["order_reference"] = {"type": "choice", "instructions": "Which candidate is the order, ticket or invoice reference this message is about?", "criteria": opts}
        maps["order_reference"] = {f"c{i}": c["value"] for i, c in enumerate(refs)}
    prods = candidates["products"]
    if prods:
        opts = {f"c{i}": p for i, p in enumerate(prods)}
        opts["none"] = "None of these is a product, plan or service name"
        q["product_name"] = {"type": "choice", "instructions": "Which candidate is the product, plan or service the customer is talking about?", "criteria": opts}
        maps["product_name"] = {f"c{i}": p for i, p in enumerate(prods)}
    amts = candidates["amounts"]
    if amts:
        opts = {f"c{i}": a for i, a in enumerate(amts)}
        opts["none"] = "No amount is in dispute"
        q["amount_in_dispute"] = {"type": "choice", "instructions": "Which candidate is the money amount in dispute or to be refunded/charged?", "criteria": opts}
        maps["amount_in_dispute"] = {f"c{i}": a for i, a in enumerate(amts)}
    return q, maps


def _choice(a):
    return {"value": a["choice"], "confidence": round(a.get("confidence", 0), 4),
            "probabilities": {k: round(v, 4) for k, v in a.get("probabilities", {}).items()}}


def parse_answers(answers, maps, candidates):
    pr = answers["priority"]
    score = float(pr["score"])
    cls = {
        "category": _choice(answers["category"]),
        "intent": _choice(answers["intent"]),
        "priority": {"score": round(score, 3), "label": PRIORITY_LEVELS[min(3, max(0, round(score)))],
                     "confidence": round(pr.get("confidence", 0), 4),
                     "probabilities": {PRIORITY_LEVELS[int(k)]: round(v, 4) for k, v in pr.get("probabilities", {}).items()}},
        "frustrated": round(answers["frustrated"]["noul"], 4),
        "time_sensitive": round(answers["time_sensitive"]["noul"], 4),
        "requested_action": _choice(answers["requested_action"]),
        "needs_human": round(answers["needs_human"]["noul"], 4),
    }
    ext = {}
    for field in ("order_reference", "product_name", "amount_in_dispute"):
        if field in answers:
            a = answers[field]
            val = maps[field].get(a["choice"])  # None when model picked "none"
            ext[field] = {"value": val, "confidence": round(a.get("confidence", 0), 4),
                          "probabilities": {(maps[field].get(k) or "none"): round(v, 4) for k, v in a.get("probabilities", {}).items()},
                          "method": "regex_candidates+subq_choice"}
        else:
            ext[field] = {"value": None, "confidence": 1.0, "probabilities": {}, "method": "no_candidates"}
    ext["emails"] = {"value": candidates["emails"], "method": "regex"}
    ext["dates"] = {"value": candidates["dates"], "method": "regex"}
    ext["candidates"] = candidates
    return cls, ext


# ---------------- fallback heuristic ----------------
_KW = {
    "account_access": ["log in", "login", "sign in", "password", "2fa", "locked", "hacked", "two-factor", "verification code"],
    "refund": ["refund", "money back", "return", "reimburse"],
    "billing": ["charge", "charged", "invoice", "billing", "payment", "card", "subscription", "price", "bill"],
    "shipping_delivery": ["delivery", "shipping", "shipped", "tracking", "package", "arrive", "courier", "parcel"],
    "technical_support": ["error", "bug", "crash", "not working", "broken", "offline", "doesn't work", "won't", "firmware", "app"],
    "complaint": ["terrible", "worst", "unacceptable", "disappointed", "complain", "rude"],
    "general_inquiry": ["how do", "do you", "can i", "question", "wondering", "what is", "?"],
}
_FRUSTRATION = ["!!", "ridiculous", "unacceptable", "worst", "terrible", "angry", "furious", "again", "still", "third time", "fed up", "scam"]
_URGENT = ["urgent", "asap", "immediately", "right now", "lawyer", "legal", "chargeback", "fraud", "hacked", "fire", "smoke", "sue"]
_INTENT_FOR = {"account_access": "cannot_login", "refund": "request_refund", "billing": "dispute_charge",
               "shipping_delivery": "track_order", "technical_support": "report_bug", "complaint": "complain_service",
               "general_inquiry": "ask_question", "other": "other"}


def heuristic_classify(text):
    low = text.lower()
    scores = {c: sum(low.count(k) for k in kws) for c, kws in _KW.items()}
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        best = "other"
    total = sum(scores.values()) or 1
    frustrated = min(1.0, 0.25 * sum(k in low for k in _FRUSTRATION) + (0.25 if text.isupper() else 0))
    urgent = any(k in low for k in _URGENT)
    pscore = 3.0 if urgent else (2.0 if frustrated >= 0.5 else 1.0)
    return {
        "category": {"value": best, "confidence": round(scores.get(best, 0) / total, 3) if best != "other" else 0.0, "probabilities": {}},
        "intent": {"value": _INTENT_FOR[best], "confidence": 0.0, "probabilities": {}},
        "priority": {"score": pscore, "label": PRIORITY_LEVELS[int(pscore)], "confidence": 0.0, "probabilities": {}},
        "frustrated": round(frustrated, 3),
        "time_sensitive": 1.0 if urgent else 0.0,
        "requested_action": {"value": "provide_information", "confidence": 0.0, "probabilities": {}},
        "needs_human": 1.0,
    }


def analyze(text, customer_name=None, channel=None, history=None, prior_customer_msgs=0, client=None):
    """Returns analysis dict with source/model/latency_ms."""
    client = client or SubQClient()
    t0 = time.perf_counter()
    cands = generate_candidates(text, customer_name)
    questions, maps = build_questions(cands)
    try:
        answers, meta = client.decide(_state(text, customer_name, channel, history or [], prior_customer_msgs), questions)
        cls, ext = parse_answers(answers, maps, cands)
        return {"source": "subq", "model": meta["model"], "latency_ms": int((time.perf_counter() - t0) * 1000),
                "usage": meta.get("usage", {}), "questions": len(questions), "classification": cls, "extraction": ext, "error": None}
    except (SubQError, KeyError, TypeError, ValueError) as e:
        cls = heuristic_classify(text)
        ext = {f: {"value": None, "confidence": 0.0, "probabilities": {}, "method": "fallback_unpicked"}
               for f in ("order_reference", "product_name", "amount_in_dispute")}
        refs = cands["references"]
        if refs:  # first regex hit is a reasonable unverified guess
            ext["order_reference"] = {"value": refs[0]["value"], "confidence": 0.0, "probabilities": {}, "method": "fallback_first_regex"}
        ext["emails"] = {"value": cands["emails"], "method": "regex"}
        ext["dates"] = {"value": cands["dates"], "method": "regex"}
        ext["candidates"] = cands
        return {"source": "fallback", "model": "keyword-heuristic", "latency_ms": int((time.perf_counter() - t0) * 1000),
                "usage": {}, "questions": 0, "classification": cls, "extraction": ext, "error": str(e)}

