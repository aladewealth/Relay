from relay.config import DEFAULT_SETTINGS as S
from relay.routing import DEPARTMENTS, route


def mk(cat="billing", conf=0.9, score=1.0, frustrated=0.1, needs_human=0.1, source="subq"):
    return {"source": source, "classification": {
        "category": {"value": cat, "confidence": conf}, "priority": {"score": score},
        "frustrated": frustrated, "needs_human": needs_human}}


def test_department_map_complete():
    for cat, dept in [("billing", "Billing"), ("technical_support", "Technical Support"), ("account_access", "Account Security"),
                      ("complaint", "Customer Success"), ("refund", "Returns & Refunds"), ("general_inquiry", "General"), ("other", "General")]:
        assert route(mk(cat), S, 1)["department"] == dept
    assert set(DEPARTMENTS.values()) == {"Billing", "Technical Support", "Account Security", "Customer Success", "Returns & Refunds", "General"}


def test_routine_case_not_escalated():
    r = route(mk(), S, 1)
    assert r["escalate"] is False and r["reasons"] == []


def test_needs_human():
    r = route(mk(needs_human=0.5), S, 1)
    assert r["escalate"] and any("Needs human" in x for x in r["reasons"])


def test_urgent():
    assert any("Urgent" in x for x in route(mk(score=2.6), S, 1)["reasons"])
    assert route(mk(score=2.4), S, 1)["escalate"] is False


def test_low_category_confidence():
    assert any("Low category confidence" in x for x in route(mk(conf=0.54), S, 1)["reasons"])
    assert route(mk(conf=0.56), S, 1)["escalate"] is False


def test_frustrated_plus_high():
    assert any("Frustrated" in x for x in route(mk(frustrated=0.8, score=2.0), S, 1)["reasons"])
    assert route(mk(frustrated=0.8, score=1.0), S, 1)["escalate"] is False  # frustrated alone is not enough
    assert route(mk(frustrated=0.2, score=2.0), S, 1)["escalate"] is False  # high alone is not enough


def test_third_unresolved_message():
    assert route(mk(), S, 2)["escalate"] is False
    r = route(mk(), S, 3)
    assert r["escalate"] and any("Repeated contact" in x for x in r["reasons"])


def test_fallback_always_escalates():
    r = route(mk(source="fallback", conf=0.0), S, 1)
    assert r["escalate"] and any("fallback" in x for x in r["reasons"])
    assert not any("Low category confidence" in x for x in r["reasons"])


def test_thresholds_from_settings():
    s = dict(S, needs_human_threshold=0.9)
    assert route(mk(needs_human=0.6), s, 1)["escalate"] is False
