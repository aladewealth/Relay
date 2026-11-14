"""API contract tests. SubQ is made unavailable (no key) so the fallback path runs end-to-end without network."""
import pytest


@pytest.fixture(autouse=True)
def no_subq(monkeypatch):
    monkeypatch.delenv("SUBQ_API_KEY", raising=False)


def post(c, text, **kw):
    body = {"customer_name": "Ann Lee", "email": "ann@example.com", "channel": "email", "text": text, **kw}
    return c.post("/api/messages", json=body)


def test_health_reports_missing_key(app_client):
    r = app_client.get("/api/health").json()
    assert r["subq"]["status"] == "missing_key" and "SUBQ_API_KEY" in r["subq"]["message"]
    assert r["status"] == "degraded"
    assert {"model", "latency_ms", "source"} <= set(r)


def test_post_message_contract(app_client):
    r = post(app_client, "I was charged twice for INV-20931, $99. Refund please.")
    assert r.status_code == 200
    j = r.json()
    for k in ("conversation_id", "message_id", "analysis", "routing", "draft", "escalated", "escalation_reasons", "model", "latency_ms", "source"):
        assert k in j
    assert j["source"] == "fallback" and j["escalated"] is True
    assert j["routing"]["department"] in ("Billing", "Returns & Refunds")
    assert j["draft"]["status"] in ("ok", "needs_review")  # guardrails unavailable -> needs_review


def test_validation(app_client):
    assert app_client.post("/api/messages", json={"customer_name": "A"}).status_code == 422
    assert post(app_client, "hi", conversation_id="nope").status_code == 404


def test_conversation_lifecycle(app_client):
    cid = post(app_client, "My Lumen Hub is offline").json()["conversation_id"]
    lst = app_client.get("/api/conversations").json()
    assert lst["count"] == 1 and lst["items"][0]["id"] == cid
    assert app_client.get("/api/conversations?status=escalated").json()["count"] == 1
    d = app_client.get(f"/api/conversations/{cid}").json()
    assert d["conversation"]["id"] == cid and len(d["messages"]) == 1 and d["messages"][0]["analysis"]
    # save edit, then send
    assert app_client.post(f"/api/conversations/{cid}/reply", json={"text": "edited", "send": False}).json()["conversation"]["draft"]["text"] == "edited"
    r = app_client.post(f"/api/conversations/{cid}/reply", json={"text": "Hello Ann, we're on it."}).json()
    assert r["conversation"]["first_response_at"]
    assert app_client.post(f"/api/conversations/{cid}/assign", json={"assignee": "maya"}).json()["conversation"]["assignee"] == "maya"
    e = app_client.post(f"/api/conversations/{cid}/escalate", json={"reason": "VIP"}).json()["conversation"]
    assert "VIP" in e["escalation_reasons"] and e["status"] == "escalated"
    assert app_client.post(f"/api/conversations/{cid}/resolve").json()["conversation"]["status"] == "resolved"
    assert app_client.get("/api/conversations/missing").status_code == 404


def test_followup_reopens_and_counts(app_client):
    cid = post(app_client, "Where is my order ORD-1234?").json()["conversation_id"]
    app_client.post(f"/api/conversations/{cid}/resolve")
    j = post(app_client, "Still not here", conversation_id=cid).json()
    assert j["conversation_id"] == cid and j["status"] != "resolved"


def test_analytics_contract(app_client):
    post(app_client, "I cannot log in!! This is ridiculous")
    a = app_client.get("/api/analytics").json()
    for k in ("volume_by_day", "volume_by_hour", "category_distribution", "priority_levels", "escalation_rate",
              "resolution_rate", "avg_first_response_seconds", "frustration_rate", "totals"):
        assert k in a
    assert a["totals"]["conversations"] == 1 and a["escalation_rate"] == 1.0


def test_settings_roundtrip(app_client):
    s = app_client.get("/api/settings").json()["settings"]
    assert "kb_text" in s and s["needs_human_threshold"] == 0.5
    r = app_client.put("/api/settings", json={"needs_human_threshold": 0.7, "kb_text": "Only policy: be nice."}).json()
    assert r["settings"]["needs_human_threshold"] == 0.7 and r["settings"]["kb_text"].startswith("Only policy")
    assert app_client.put("/api/settings", json={"needs_human_threshold": 3}).status_code == 422


def test_frontend_served(app_client):
    assert app_client.get("/").status_code == 200
    assert app_client.get("/static/app.js").status_code == 200
