"""Environment configuration. Read at call time so tests can monkeypatch env."""
import os

SUBQ_BASE_URL = os.environ.get("SUBQ_BASE_URL", "https://api.subq.ai/v1")
SUBQ_MODEL = os.environ.get("SUBQ_MODEL", "subq-snap-1.2")


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def subq_key():
    return env("SUBQ_API_KEY")


def db_path():
    return env("RELAY_DB_PATH", "/tmp/relay.db")


DEFAULT_KB = """Company: Lumen Home (smart home devices and the Lumen Plus subscription).

PRODUCTS
- Lumen Hub ($129), Lumen Cam Pro ($199), Lumen Door Sensor ($39).
- Lumen Plus subscription: $9.99/month or $99/year (cloud video history, 24/7 alerts).

REFUNDS & RETURNS
- Devices can be returned within 30 days of delivery for a full refund if in original packaging. Customer must start the return from Account > Orders; we email a prepaid label.
- Refunds go back to the original payment method within 5-10 business days after the return is received.
- Lumen Plus monthly plans are not refunded for partial months. Annual plans can be refunded pro-rata within the first 14 days only.
- Duplicate or erroneous charges confirmed by Billing are always refunded in full. Agents must verify the duplicate charge before confirming a refund.
- Agents may NOT promise credits, discounts, or compensation. Goodwill credits require approval by a Customer Success lead.

SHIPPING
- Standard shipping 3-7 business days; Express 1-2 business days. Tracking link is emailed when the order ships.
- If tracking shows no movement for 5 business days, we open a carrier investigation (takes up to 3 business days).
- Lost packages confirmed by the carrier are reshipped free of charge.

ACCOUNT & SECURITY
- Password reset: "Forgot password" on the login page; links expire after 30 minutes.
- If a customer is locked out after 5 failed attempts, the lock lasts 15 minutes.
- Two-factor problems or suspected account takeover go to Account Security; never change an account email over chat without identity verification.

SUBSCRIPTIONS
- Customers can cancel or change plans any time in Settings > Subscription; cancellation takes effect at the end of the current billing period.
- Plan upgrades are charged pro-rata immediately.

TECHNICAL SUPPORT
- First steps for offline devices: power-cycle the device, confirm 2.4 GHz Wi-Fi, update the Lumen app.
- Known issue (Nov 2026): Cam Pro firmware 4.2.0 can drop night-vision; fix in firmware 4.2.1 rolling out.

SUPPORT HOURS
- Chat and email: Monday-Saturday, 8am-8pm WAT. Typical first response within 4 business hours."""

DEFAULT_SETTINGS = {
    "company_name": "Lumen Home",
    "kb_text": DEFAULT_KB,
    "needs_human_threshold": 0.5,
    "category_confidence_min": 0.55,
    "frustrated_threshold": 0.5,
    "high_priority_score": 1.5,      # score >= this counts as high (levels: 0 low,1 medium,2 high,3 urgent)
    "urgent_priority_score": 2.5,    # round(score) == 3
    "max_unresolved_messages": 3,    # 3rd unresolved customer message escalates
    "guardrail_threshold": 0.5,
}
