"""Orchestrates: persist -> analyze (1 SubQ call) -> route -> draft (generator) -> guardrail (1 SubQ call)."""
import queue
import threading
import time

from . import db
from .classifier import analyze
from .drafting import draft_reply
from .routing import route

_draft_q = queue.Queue()
_pending = {}  # cid -> list of Events waiting on that draft; one queued job per conversation
_pending_lock = threading.Lock()
_worker = None
_worker_lock = threading.Lock()


def _ensure_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_draft_loop, daemon=True)
            _worker.start()


def _draft_loop():
    while True:
        cid = _draft_q.get()
        with _pending_lock:
            waiters = _pending.pop(cid, [])  # later requests for this cid are served by this run (it reads the latest thread)
        try:
            run_draft(cid)
        except Exception as e:  # noqa: BLE001
            db.update_conversation(cid, draft={"status": "failed", "error": f"{type(e).__name__}: {e}", "text": None},
                                   draft_status="failed")
        finally:
            for w in waiters:
                w.set()
            _draft_q.task_done()


def queue_depth():
    return _draft_q.qsize()


def _unresolved_customer_count(cid, conv):
    since = conv.get("last_resolved_at") or ""
    return sum(1 for m in db.messages(cid) if m["role"] == "customer" and m["created_at"] >= since)


def escalate(cid, reasons, actor="system"):
    conv = db.get_conversation(cid)
    existing = conv.get("escalation_reasons") or []
    merged = existing + [r for r in reasons if r not in existing]
    db.update_conversation(cid, escalated=True, status="escalated", escalation_reasons=merged)
    db.add_event(cid, "escalated", {"reasons": reasons, "by": actor})


def run_draft(cid):
    conv = db.get_conversation(cid)
    msgs = db.messages(cid)
    last = next((m for m in reversed(msgs) if m["role"] == "customer" and m.get("analysis")), None)
    if last is None:
        return None
    settings = db.get_settings()
    db.update_conversation(cid, draft_status="generating")
    d = draft_reply(conv, [{"role": m["role"], "text": m["text"]} for m in msgs], last["analysis"], conv["routing"], settings)
    db.update_conversation(cid, draft=d, draft_status=d["status"])
    if d["status"] == "failed":
        escalate(cid, [f"No AI draft: generator failed ({d['error'][:120]})"])
    db.add_event(cid, "draft", {"status": d["status"], "latency_ms": d["latency_ms"], "model": d["model"]})
    return d


def request_draft(cid, wait=True):
    db.update_conversation(cid, draft_status="pending")
    _ensure_worker()
    done = threading.Event()
    with _pending_lock:
        queued = cid in _pending
        _pending.setdefault(cid, []).append(done)
    if not queued:
        _draft_q.put(cid)
    if wait:
        done.wait()
    return db.get_conversation(cid)["draft"]


def process_message(customer_name, email, channel, text, conversation_id=None, wait_draft=True):
    t0 = time.perf_counter()
    conv = db.get_conversation(conversation_id) if conversation_id else None
    if conversation_id and conv is None:
        raise KeyError(conversation_id)
    if conv is None:
        subject = text.strip().split("\n")[0][:80]
        cid = db.create_conversation(customer_name, email, channel, subject)
    else:
        cid = conversation_id
        if conv["status"] == "resolved":
            db.update_conversation(cid, status="escalated" if conv["escalated"] else "open", resolved_at=None)
            db.add_event(cid, "reopened", {"by": "customer"})
    conv = db.get_conversation(cid)
    history = [{"role": m["role"], "text": m["text"]} for m in db.messages(cid)]
    prior = _unresolved_customer_count(cid, conv)
    mid = db.add_message(cid, "customer", text, author=customer_name)
    analysis = analyze(text, customer_name, channel, history, prior)
    db.set_message_analysis(mid, analysis)

    settings = db.get_settings()
    routing = route(analysis, settings, prior + 1)
    cls = analysis["classification"]
    db.update_conversation(cid, department=routing["department"], category=routing["category"],
                           priority_label=cls["priority"]["label"], priority_score=cls["priority"]["score"],
                           frustrated=cls["frustrated"], routing=routing)
    if routing["escalate"]:
        escalate(cid, routing["reasons"])
    db.add_event(cid, "analyzed", {"message_id": mid, "source": analysis["source"], "latency_ms": analysis["latency_ms"]})

    draft = request_draft(cid, wait=wait_draft)
    conv = db.get_conversation(cid)
    return {
        "conversation_id": cid, "message_id": mid, "status": conv["status"],
        "analysis": analysis, "routing": routing,
        "escalated": conv["escalated"], "escalation_reasons": conv["escalation_reasons"] or [],
        "draft": draft if wait_draft else {"status": conv["draft_status"]},
        "source": analysis["source"], "model": analysis["model"],
        "latency_ms": int((time.perf_counter() - t0) * 1000),
    }


def recover_stuck_drafts():
    """Re-queue drafts that were pending/generating when the process stopped."""
    n = 0
    for c in db.list_conversations(limit=100000):
        if c.get("draft_status") in ("pending", "generating"):
            request_draft(c["id"], wait=False)
            n += 1
    return n
