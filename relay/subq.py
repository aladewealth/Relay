"""Thin SubQ Decisions API client with retry/backoff on 429/5xx."""
import time

import httpx

from . import config


class SubQError(Exception):
    def __init__(self, message, status=None, error_type=None):
        super().__init__(message)
        self.status = status
        self.error_type = error_type


class SubQClient:
    def __init__(self, api_key=None, base_url=None, model=None, timeout=30.0, retries=3):
        self.api_key = api_key if api_key is not None else config.subq_key()
        self.base_url = (base_url or config.SUBQ_BASE_URL).rstrip("/")
        self.model = model or config.SUBQ_MODEL
        self.timeout = timeout
        self.retries = retries

    @property
    def configured(self):
        return bool(self.api_key)

    def decide(self, state, questions):
        """POST /systemone. Returns (answers, meta). Raises SubQError."""
        if not self.api_key:
            raise SubQError("SubQ key missing (set the SUBQ_API_KEY secret)", status=None, error_type="missing_key")
        if not 1 <= len(questions) <= 128:
            raise SubQError(f"questions must be 1-128, got {len(questions)}", error_type="client")
        body = {"model": self.model, "state": state, "questions": questions}
        delay = 1.0
        last = None
        t0 = time.perf_counter()
        for attempt in range(self.retries + 1):
            try:
                r = httpx.post(f"{self.base_url}/systemone", json=body, timeout=self.timeout,
                               headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.HTTPError as e:
                last = SubQError(f"network error: {e}", error_type="network")
            else:
                if r.status_code == 200:
                    data = r.json()
                    return data["answers"], {"model": data.get("model", self.model),
                                             "usage": data.get("usage", {}),
                                             "latency_ms": int((time.perf_counter() - t0) * 1000)}
                try:
                    d = r.json().get("detail", {})
                    msg, et = d.get("message", r.text[:200]), d.get("error_type")
                except Exception:
                    msg, et = r.text[:200], None
                last = SubQError(f"{r.status_code} {et}: {msg}", status=r.status_code, error_type=et)
                if r.status_code not in (429,) and r.status_code < 500:
                    raise last  # 400/401/402/403/422: retrying won't help
            if attempt < self.retries:
                time.sleep(delay)
                delay *= 2
        raise last
