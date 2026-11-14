"""Routing and escalation rules live in code; SubQ only supplies the judgments."""

DEPARTMENTS = {
    "billing": "Billing",
    "technical_support": "Technical Support",
    "account_access": "Account Security",
    "complaint": "Customer Success",
    "refund": "Returns & Refunds",
    "shipping_delivery": "Returns & Refunds",  # fulfilment issues are owned by Returns & Refunds
    "general_inquiry": "General",
    "other": "General",
}


def route(analysis, settings, unresolved_customer_msgs):
    """unresolved_customer_msgs counts customer messages since the conversation was last resolved, including this one."""
    cls = analysis["classification"]
    cat = cls["category"]["value"]
    dept = DEPARTMENTS.get(cat, "General")
    reasons = []
    if analysis.get("source") == "fallback":
        reasons.append("SubQ unavailable: classified by keyword fallback")
    nh = cls["needs_human"]
    if nh >= settings["needs_human_threshold"]:
        reasons.append(f"Needs human judgment (p={nh:.2f} ≥ {settings['needs_human_threshold']})")
    ps = cls["priority"]["score"]
    if ps >= settings["urgent_priority_score"]:
        reasons.append(f"Urgent priority (score {ps:.2f})")
    cc = cls["category"]["confidence"]
    if cc < settings["category_confidence_min"] and analysis.get("source") != "fallback":
        reasons.append(f"Low category confidence ({cc:.2f} < {settings['category_confidence_min']})")
    fr = cls["frustrated"]
    if fr >= settings["frustrated_threshold"] and settings["high_priority_score"] <= ps < settings["urgent_priority_score"]:
        reasons.append(f"Frustrated customer (p={fr:.2f}) with high priority (score {ps:.2f})")
    if unresolved_customer_msgs >= settings["max_unresolved_messages"]:
        reasons.append(f"Repeated contact: message #{unresolved_customer_msgs} without resolution")
    return {"department": dept, "category": cat, "escalate": bool(reasons), "reasons": reasons}
