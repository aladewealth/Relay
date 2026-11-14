import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeSubQ:
    """Deterministic stand-in for SubQClient used only in unit tests (never in the app)."""

    def __init__(self, fail=False, overrides=None):
        self.fail = fail
        self.overrides = overrides or {}
        self.calls = []
        self.configured = True
        self.model = "fake-snap"

    def decide(self, state, questions):
        from relay.subq import SubQError
        self.calls.append((state, questions))
        if self.fail:
            raise SubQError("503 service_unavailable: down", status=503)
        ans = {}
        for name, q in questions.items():
            if q["type"] == "noul":
                ans[name] = {"type": "noul", "noul": self.overrides.get(name, 0.1)}
            elif q["type"] == "score":
                s = self.overrides.get(name, 1.0)
                ans[name] = {"type": "score", "score": s, "legend": {str(i): l for i, l in enumerate(q["criteria"])},
                             "probabilities": {str(i): (1.0 if i == round(s) else 0.0) for i in range(len(q["criteria"]))},
                             "confidence": 0.9}
            else:
                opts = list(q["criteria"])
                pick = self.overrides.get(name, opts[0])
                ans[name] = {"type": "choice", "choice": pick, "probabilities": {o: (0.9 if o == pick else 0.1 / (len(opts) - 1)) for o in opts},
                             "confidence": self.overrides.get(name + "_conf", 0.9)}
        return ans, {"model": self.model, "usage": {"input_tokens": 1}, "latency_ms": 1}


class FakeGen:
    name = "fake"
    model = "fake-gen"

    def __init__(self, texts=None, fail=False):
        self.texts = list(texts or ["Thanks for reaching out. Our team will review your request. Lumen Home Support"])
        self.fail = fail
        self.calls = 0

    def status(self):
        return {"provider": self.name, "model": self.model, "ready": True, "error": None}

    def generate(self, messages, max_tokens=320):
        from relay.generators import GeneratorError
        self.calls += 1
        if self.fail:
            raise GeneratorError("boom")
        return self.texts[min(self.calls - 1, len(self.texts) - 1)]


@pytest.fixture
def fake_subq():
    return FakeSubQ


@pytest.fixture
def fake_gen():
    return FakeGen


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("RELAY_PRELOAD_MODEL", "0")
    from fastapi.testclient import TestClient

    from relay import api, generators
    generators.set_generator(FakeGen())
    api._health_cache.update(at=0, value=None)
    with TestClient(api.create_app()) as c:
        yield c
    generators.set_generator(None)
