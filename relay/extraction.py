"""Candidate-span generation in code; SubQ picks among candidates (see classifier.py)."""
import re

MAX_CANDIDATES = 12

_REF_PATTERNS = [
    ("order", re.compile(r"#[A-Z]{1,4}-?\d{3,}\b")),
    ("order", re.compile(r"#\d{4,}\b")),
    ("order", re.compile(r"\b(?:ORD|ORDER|PO|SO)[-_#]?\d{3,}[A-Z0-9-]*\b", re.I)),
    ("ticket", re.compile(r"\b(?:TKT|TICKET|CASE|REQ)[-_#]?\d{3,}\b", re.I)),
    ("invoice", re.compile(r"\b(?:INV|INVOICE)[-_#]?\d{3,}[A-Z0-9-]*\b", re.I)),
    ("invoice", re.compile(r"\binvoice\s+(?:number|no\.?|#)\s*:?\s*([A-Z0-9-]{4,})\b", re.I)),
    ("tracking", re.compile(r"\b1Z[0-9A-Z]{16}\b")),
    ("tracking", re.compile(r"\btracking\s+(?:number|no\.?|#)\s*:?\s*([A-Z0-9]{8,})\b", re.I)),
]
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_CUR_SYM = r"[$€£₦¥]"
_CUR_CODE = r"(?:USD|EUR|GBP|NGN|CAD|AUD|dollars?|euros?|pounds?|naira)"
AMOUNT_RES = [
    re.compile(rf"(?:{_CUR_CODE}\s?)?{_CUR_SYM}\s?\d{{1,3}}(?:,\d{{3}})*(?:\.\d{{1,2}})?|{_CUR_SYM}\s?\d+(?:\.\d{{1,2}})?", re.I),
    re.compile(rf"\b\d{{1,3}}(?:,\d{{3}})*(?:\.\d{{1,2}})?\s?{_CUR_CODE}\b", re.I),
    re.compile(rf"\b{_CUR_CODE}\s?\d{{1,3}}(?:,\d{{3}})*(?:\.\d{{1,2}})?\b", re.I),
]
_MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
DATE_RES = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),
    re.compile(rf"\b{_MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b", re.I),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTHS}(?:\s+\d{{4}})?\b", re.I),
    re.compile(r"\b(?:yesterday|today|tomorrow|last\s+(?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
               r"(?:on\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|\d+\s+(?:days?|weeks?|months?)\s+ago)\b", re.I),
]
_STOP = {
    "i", "hi", "hello", "hey", "dear", "thanks", "thank", "please", "the", "my", "we", "our", "you", "your", "it", "this",
    "that", "and", "but", "or", "so", "if", "is", "are", "was", "can", "could", "would", "should", "what", "why", "how",
    "when", "where", "who", "also", "just", "still", "now", "again", "ok", "okay", "sorry", "regards", "best", "cheers",
    "a", "an", "to", "for", "on", "in", "of", "at", "no", "not", "yes", "they", "he", "she", "me", "its", "do", "does",
    "did", "have", "has", "had", "been", "be", "will", "all", "any", "after", "before", "since", "then", "there",
    "january", "february", "march", "april", "june", "july", "august", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "usd", "eur", "gbp", "ngn", "asap",
    "urgent", "help", "support", "team", "order", "invoice", "ticket", "customer", "service",
}
_TOKEN = r"(?:[A-Z][a-zA-Z0-9+]*|[0-9]+(?:\.[0-9]+)?[A-Za-z]*|[A-Z0-9]{2,})"
PRODUCT_RE = re.compile(rf"\b{_TOKEN}(?:[ -]{_TOKEN}){{0,3}}\b")
PLAN_RE = re.compile(r"\b([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?\s(?:plan|subscription|tier|membership))\b")


def _dedupe(items):
    seen, out = set(), []
    for x in items:
        k = x.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(x.strip())
    return out[:MAX_CANDIDATES]


def reference_candidates(text):
    out = []
    for kind, rx in _REF_PATTERNS:
        for m in rx.finditer(text):
            val = m.group(1) if m.groups() else m.group(0)
            out.append({"value": val.rstrip(".,;"), "kind": kind})
    seen, res = set(), []
    for c in out:
        k = c["value"].lower().lstrip("#")
        if k not in seen:
            seen.add(k)
            res.append(c)
    return res[:MAX_CANDIDATES]


def amount_candidates(text):
    vals = []
    for rx in AMOUNT_RES:
        vals += [m.group(0) for m in rx.finditer(text)]
    # drop amounts that are substrings of longer matched amounts
    vals = _dedupe(vals)
    return [v for v in vals if not any(v != o and v in o for o in vals)]


def date_candidates(text):
    vals = []
    for rx in DATE_RES:
        vals += [m.group(0) for m in rx.finditer(text)]
    vals = _dedupe(vals)
    return [v for v in vals if not any(v != o and v.lower() in o.lower() for o in vals)]


def product_candidates(text, exclude=()):
    excl = {e.lower() for e in exclude if e}
    refs = {c["value"].lower().lstrip("#") for c in reference_candidates(text)}
    out = [m.group(1) for m in PLAN_RE.finditer(text)]
    for m in PRODUCT_RE.finditer(text):
        span = m.group(0)
        words = span.split()
        # trim leading/trailing stopwords ("Hi Lumen Hub" -> "Lumen Hub")
        while words and words[0].lower().strip("-") in _STOP:
            words.pop(0)
        while words and words[-1].lower().strip("-") in _STOP:
            words.pop()
        if not words:
            continue
        cand = " ".join(words)
        low = cand.lower()
        if low in excl or low.lstrip("#") in refs or any(low in e for e in excl):
            continue
        if len(words) == 1 and (len(cand) < 3 or cand.isdigit()):
            continue
        # a lone capitalised word at sentence start is usually not a product
        start = m.start() + span.find(words[0])
        prev = text[:start].rstrip()
        if len(words) == 1 and (not prev or prev[-1] in ".!?\n") and not any(ch.isdigit() for ch in cand) and not cand.isupper():
            continue
        if re.fullmatch(r"[\d.,]+", cand):
            continue
        out.append(cand)
    return _dedupe(out)


def generate_candidates(text, customer_name=None):
    refs = reference_candidates(text)
    amounts = amount_candidates(text)
    dates = date_candidates(text)
    emails = _dedupe(EMAIL_RE.findall(text))
    exclude = list(emails) + amounts + dates + ([customer_name] if customer_name else [])
    if customer_name:
        exclude += customer_name.split()
    products = product_candidates(text, exclude=exclude)
    return {"references": refs, "amounts": amounts, "dates": dates, "emails": emails, "products": products}
