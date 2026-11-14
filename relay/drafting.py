"""Grounded reply drafting + SubQ guardrail check, with one stricter regeneration."""
import json
import time

from .generators import GeneratorError, get_generator
from .subq import SubQClient, SubQError

RULES = """Rules:
- Be professional, warm and empathetic; acknowledge the customer's situation in one sentence.
- Be concise: 60-140 words. Plain text, no markdown, no subject line, no placeholders like [Name].
- Use ONLY facts from the KNOWLEDGE BASE. Never invent policies, timelines, prices or procedures.
- Never promise a refund, credit, discount or compensation unless the KNOWLEDGE BASE explicitly guarantees it for this exact situation; otherwise say the team will review it.
- If something needs checking, say what the next step is and who will handle it.
- Reference the customer's order/product/amount when known.
- Sign off as "{company} Support"."""

STRICT = """STRICT MODE: a previous draft failed review ({failed}). This time:
- Do NOT mention refunds, credits, compensation or money back as a promise. You may only say the request has been passed to the {dept} team for review.
- Do NOT state any policy, timeline or number unless it appears word-for-word in the KNOWLEDGE BASE.
- Keep a calm, professional tone. 60-110 words."""


def system_prompt(settings):
    return ("You are a customer support agent writing a reply draft for a human agent to approve.\n"
            + RULES.format(company=settings["company_name"]) + f"\n\nKNOWLEDGE BASE:\n{settings['kb_text']}")


def build_messages(conv, history, analysis, routing, settings, strict_failed=None):
    cls = analysis["classification"]
    ext = analysis["extraction"]
    facts = {
        "category": cls["category"]["value"], "intent": cls["intent"]["value"],
        "priority": cls["priority"]["label"], "customer_seems_frustrated": cls["frustrated"] >= 0.5,
        "requested_action": cls["requested_action"]["value"], "routed_to": routing["department"],
        "escalated_to_human": routing["escalate"],
        "order_reference": ext.get("order_reference", {}).get("value"),
        "product": ext.get("product_name", {}).get("value"),
        "amount_in_dispute": ext.get("amount_in_dispute", {}).get("value"),
    }
    # System prompt = rules + KB only, so it is byte-identical across calls: llama.cpp reuses its KV cache
    # (prefix match) and only the case-specific user turn is evaluated. Strict mode goes in the user turn.
    system = system_prompt(settings)
    convo = "\n".join(f"{'Customer' if m['role'] == 'customer' else 'Support'}: {m['text']}" for m in history[-8:])
    user = (f"CASE FACTS (from automated analysis): {json.dumps(facts)}\n"
            + ("Note: this case was escalated to a human specialist; say a specialist from the "
               f"{routing['department']} team will follow up.\n" if routing["escalate"] else "")
            + f"\nCONVERSATION with {conv.get('customer_name') or 'the customer'}:\n{convo}\n\n"
            + (STRICT.format(failed=", ".join(strict_failed), dept=routing["department"]) + "\n\n" if strict_failed else "")
            + "Write the reply to the customer's latest message now.")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


GUARD_QUESTIONS = {
    "promises_refund_or_compensation": {
        "type": "noul",
        "instructions": "Does the DRAFT promise or confirm a refund, credit, discount or compensation that the KNOWLEDGE BASE does not explicitly guarantee for this situation? (Saying a request will be reviewed is NOT a promise.)",
        "criteria": {"true": "Promises money back, credit or compensation not guaranteed by the KB",
                     "false": "No unsupported promise; only reviews, next steps or KB-guaranteed outcomes"}},
    "states_policy_not_in_kb": {
        "type": "noul",
        "instructions": "Does the DRAFT state a policy, timeline, price, fee or procedure that is NOT supported by the KNOWLEDGE BASE?",
        "criteria": {"true": "Contains an invented or contradicting policy/fact",
                     "false": "Every policy or fact stated is in the KB"}},
    "tone_professional": {
        "type": "noul",
        "instructions": "Is the DRAFT professional, polite and empathetic in tone, suitable to send to a customer?",
        "criteria": {"true": "Professional and empathetic", "false": "Rude, dismissive, sloppy or inappropriate"}},
}


def guardrail_check(draft, customer_text, settings, client=None):
    client = client or SubQClient()
    t0 = time.perf_counter()
    state = {"KNOWLEDGE_BASE": settings["kb_text"], "CUSTOMER_MESSAGE": customer_text, "DRAFT": draft}
    th = settings["guardrail_threshold"]
    try:
        ans, meta = client.decide(state, GUARD_QUESTIONS)
    except SubQError as e:
        return {"source": "unavailable", "passed": False, "failed": ["guardrail_unavailable"], "error": str(e),
                "scores": {}, "model": None, "latency_ms": int((time.perf_counter() - t0) * 1000)}
    scores = {k: round(v["noul"], 4) for k, v in ans.items()}
    failed = []
    if scores["promises_refund_or_compensation"] >= th:
        failed.append("promises_refund_or_compensation")
    if scores["states_policy_not_in_kb"] >= th:
        failed.append("states_policy_not_in_kb")
    if scores["tone_professional"] < th:
        failed.append("tone_not_professional")
    return {"source": "subq", "passed": not failed, "failed": failed, "scores": scores, "error": None,
            "model": meta["model"], "latency_ms": meta["latency_ms"]}


def draft_reply(conv, history, analysis, routing, settings, client=None, generator=None):
    """Returns draft dict: {status: ok|needs_review|failed, text, attempts:[...], model, latency_ms, source}."""
    gen = generator or get_generator()
    t0 = time.perf_counter()
    attempts = []
    customer_text = next((m["text"] for m in reversed(history) if m["role"] == "customer"), "")
    failed = None
    text = None
    guard = None
    for attempt in (1, 2):
        msgs = build_messages(conv, history, analysis, routing, settings, strict_failed=failed)
        g0 = time.perf_counter()
        try:
            text = gen.generate(msgs)
        except GeneratorError as e:
            attempts.append({"attempt": attempt, "error": str(e), "gen_ms": int((time.perf_counter() - g0) * 1000)})
            return {"status": "failed", "text": None, "error": str(e), "attempts": attempts, "guardrails": guard,
                    "source": gen.name, "model": gen.model, "latency_ms": int((time.perf_counter() - t0) * 1000)}
        gen_ms = int((time.perf_counter() - g0) * 1000)
        guard = guardrail_check(text, customer_text, settings, client=client)
        attempts.append({"attempt": attempt, "strict": attempt == 2, "gen_ms": gen_ms, "text": text, "guardrails": guard})
        if guard["passed"]:
            break
        failed = guard["failed"]
        if guard["source"] == "unavailable":
            break  # can't verify; regenerating won't help
    status = "ok" if guard and guard["passed"] else "needs_review"
    return {"status": status, "text": text, "error": None, "attempts": attempts, "guardrails": guard,
            "source": gen.name, "model": gen.model, "latency_ms": int((time.perf_counter() - t0) * 1000)}
