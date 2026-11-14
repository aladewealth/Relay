from relay import classifier, drafting
from relay.config import DEFAULT_SETTINGS as S


def test_single_subq_call_with_all_questions(fake_subq):
    fs = fake_subq(overrides={"order_reference": "c0", "amount_in_dispute": "c0", "product_name": "c0"})
    a = classifier.analyze("Order #A-4471 for my Lumen Hub cost $129 and never came", "Ann", "email", client=fs)
    assert len(fs.calls) == 1
    qs = fs.calls[0][1]
    for q in ("category", "intent", "priority", "frustrated", "time_sensitive", "requested_action", "needs_human",
              "order_reference", "product_name", "amount_in_dispute"):
        assert q in qs
    assert all("none" in qs[q]["criteria"] for q in ("order_reference", "product_name", "amount_in_dispute"))
    assert a["source"] == "subq" and a["model"] == "fake-snap" and "latency_ms" in a
    assert a["extraction"]["order_reference"]["value"] == "#A-4471"
    assert a["extraction"]["amount_in_dispute"]["value"] == "$129"


def test_extraction_none_choice(fake_subq):
    a = classifier.analyze("Order #A-4471 is fine", "Ann", "email", client=fake_subq(overrides={"order_reference": "none"}))
    assert a["extraction"]["order_reference"]["value"] is None


def test_subq_failure_uses_fallback(fake_subq):
    a = classifier.analyze("I was charged twice on my card, refund please!!", "Ann", "email", client=fake_subq(fail=True))
    assert a["source"] == "fallback" and a["model"] == "keyword-heuristic"
    assert a["classification"]["needs_human"] == 1.0
    assert a["error"]
    assert a["classification"]["category"]["value"] in ("billing", "refund")


def test_missing_key_falls_back(monkeypatch):
    monkeypatch.delenv("SUBQ_API_KEY", raising=False)
    a = classifier.analyze("I cannot log in to my account", "Ann", "chat")
    assert a["source"] == "fallback" and "key missing" in a["error"]
    assert a["classification"]["category"]["value"] == "account_access"


def _analysis(fake_subq):
    return classifier.analyze("My Lumen Hub is offline", "Ann", "chat", client=fake_subq())


def test_draft_passes_guardrails(fake_subq, fake_gen):
    fs = fake_subq(overrides={"promises_refund_or_compensation": 0.1, "states_policy_not_in_kb": 0.1, "tone_professional": 0.95})
    a = _analysis(fake_subq)
    g = fake_gen()
    d = drafting.draft_reply({"customer_name": "Ann"}, [{"role": "customer", "text": "My Lumen Hub is offline"}], a,
                             {"department": "Technical Support", "escalate": False}, S, client=fs, generator=g)
    assert d["status"] == "ok" and g.calls == 1 and d["text"]
    assert {"model", "latency_ms", "source"} <= set(d)


def test_guardrail_fail_regenerates_once_then_needs_review(fake_subq, fake_gen):
    fs = fake_subq(overrides={"promises_refund_or_compensation": 0.9, "states_policy_not_in_kb": 0.1, "tone_professional": 0.95})
    g = fake_gen(texts=["We will refund you $500 now!", "Still refunding you!"])
    d = drafting.draft_reply({"customer_name": "Ann"}, [{"role": "customer", "text": "x"}], _analysis(fake_subq),
                             {"department": "Billing", "escalate": False}, S, client=fs, generator=g)
    assert g.calls == 2 and d["status"] == "needs_review"
    assert d["attempts"][1]["strict"] is True
    assert "promises_refund_or_compensation" in d["guardrails"]["failed"]


def test_strict_prompt_used_on_retry(fake_subq):
    a = _analysis(fake_subq)
    m = drafting.build_messages({"customer_name": "Ann"}, [{"role": "customer", "text": "x"}], a,
                                {"department": "Billing", "escalate": False}, S, strict_failed=["promises_refund_or_compensation"])
    assert "STRICT MODE" in m[1]["content"]
    m0 = drafting.build_messages({"customer_name": "Ann"}, [{"role": "customer", "text": "x"}], a,
                                 {"department": "Billing", "escalate": False}, S)
    assert m0[0]["content"] == m[0]["content"]  # system prefix stays identical (KV-cache reuse)


def test_generator_failure_returns_no_draft(fake_subq, fake_gen):
    d = drafting.draft_reply({"customer_name": "Ann"}, [{"role": "customer", "text": "x"}], _analysis(fake_subq),
                             {"department": "Billing", "escalate": False}, S, client=fake_subq(), generator=fake_gen(fail=True))
    assert d["status"] == "failed" and d["text"] is None and d["error"]


def test_guardrail_unavailable_marks_needs_review(fake_subq, fake_gen):
    g = fake_gen()
    d = drafting.draft_reply({"customer_name": "Ann"}, [{"role": "customer", "text": "x"}], _analysis(fake_subq),
                             {"department": "Billing", "escalate": False}, S, client=fake_subq(fail=True), generator=g)
    assert d["status"] == "needs_review" and g.calls == 1
