"""FastAPI app: REST API under /api + static dashboard at /."""
import os
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, config, db, pipeline, samples
from .generators import cpu_info, get_generator
from .subq import SubQClient, SubQError

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def meta(t0, source="relay", model=None, **extra):
    return {"source": source, "model": model, "latency_ms": int((time.perf_counter() - t0) * 1000), **extra}


class MessageIn(BaseModel):
    customer_name: str = Field(..., min_length=1, max_length=120)
    email: Optional[str] = Field(None, max_length=200)
    channel: str = Field("email", max_length=40)
    text: str = Field(..., min_length=1, max_length=8000)
    conversation_id: Optional[str] = None


class ReplyIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=8000)
    send: bool = True           # False = just save the edited draft
    author: str = "Agent"


class EscalateIn(BaseModel):
    reason: str = "Escalated manually by agent"


class AssignIn(BaseModel):
    assignee: Optional[str] = None


class SettingsIn(BaseModel):
    company_name: Optional[str] = None
    kb_text: Optional[str] = Field(None, max_length=60000)
    needs_human_threshold: Optional[float] = Field(None, ge=0, le=1)
    category_confidence_min: Optional[float] = Field(None, ge=0, le=1)
    frustrated_threshold: Optional[float] = Field(None, ge=0, le=1)
    high_priority_score: Optional[float] = Field(None, ge=0, le=3)
    urgent_priority_score: Optional[float] = Field(None, ge=0, le=3)
    max_unresolved_messages: Optional[int] = Field(None, ge=1, le=50)
    guardrail_threshold: Optional[float] = Field(None, ge=0, le=1)


_health_cache = {"at": 0, "value": None}


def subq_health(force=False):
    if not force and _health_cache["value"] and time.time() - _health_cache["at"] < 60:
        return _health_cache["value"]
    c = SubQClient(retries=0, timeout=10)
    if not c.configured:
        v = {"status": "missing_key", "reachable": False, "message": "SubQ key missing: add the SUBQ_API_KEY secret in Space settings. Classification runs on the keyword fallback and every case is escalated until then.", "model": c.model}
    else:
        t0 = time.perf_counter()
        try:
            c.decide("ping", {"ok": {"type": "noul", "instructions": "Is this a ping?"}})
            v = {"status": "ok", "reachable": True, "model": c.model, "latency_ms": int((time.perf_counter() - t0) * 1000)}
        except SubQError as e:
            v = {"status": "error", "reachable": False, "model": c.model, "message": str(e), "http_status": e.status}
    _health_cache.update(at=time.time(), value=v)
    return v


def _warm_generator():
    g = get_generator()
    if hasattr(g, "start_loading") and config.env("RELAY_PRELOAD_MODEL", "1") == "1":
        from .drafting import system_prompt
        g.warm_system = system_prompt(db.get_settings())
        g.start_loading()
    pipeline.recover_stuck_drafts()


@asynccontextmanager
async def _lifespan(app):
    _warm_generator()
    yield


def create_app():
    db.init()
    app = FastAPI(title="Relay API", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json",
                  lifespan=_lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def _get(cid):
        c = db.get_conversation(cid)
        if not c:
            raise HTTPException(404, {"error": "conversation not found", "id": cid})
        return c

    @app.get("/api/health")
    def health(force: bool = False):
        t0 = time.perf_counter()
        sq = subq_health(force)
        gs = get_generator().status()
        ok = sq["status"] == "ok" and gs.get("ready")
        return {"status": "ok" if ok else "degraded", "version": __version__, "subq": sq, "generator": gs,
                "draft_queue": pipeline.queue_depth(), "db": config.db_path(), "system": cpu_info(), **meta(t0, "health", sq.get("model"))}

    @app.post("/api/messages")
    def post_message(m: MessageIn, wait_draft: bool = True):
        try:
            r = pipeline.process_message(m.customer_name, m.email, m.channel, m.text, m.conversation_id, wait_draft)
        except KeyError:
            raise HTTPException(404, {"error": "conversation not found", "id": m.conversation_id})
        return r

    @app.get("/api/conversations")
    def list_convs(status: Optional[str] = None, department: Optional[str] = None, escalated: Optional[bool] = None):
        t0 = time.perf_counter()
        items = db.list_conversations(status, department, escalated)
        for c in items:
            d = c.pop("draft", None) or {}
            c["draft_preview"] = (d.get("text") or "")[:160]
        return {"items": items, "count": len(items), **meta(t0, "db")}

    @app.get("/api/conversations/{cid}")
    def get_conv(cid: str):
        t0 = time.perf_counter()
        c = _get(cid)
        return {"conversation": c, "messages": db.messages(cid), "events": db.events(cid), **meta(t0, "db")}

    @app.post("/api/conversations/{cid}/reply")
    def reply(cid: str, body: ReplyIn):
        t0 = time.perf_counter()
        c = _get(cid)
        if not body.send:
            d = dict(c.get("draft") or {})
            d.update(text=body.text, edited_by=body.author, status=d.get("status") or "edited")
            db.update_conversation(cid, draft=d)
            db.add_event(cid, "draft_edited", {"by": body.author})
            return {"conversation": db.get_conversation(cid), **meta(t0, "agent")}
        ai_text = (c.get("draft") or {}).get("text")
        mid = db.add_message(cid, "agent", body.text, author=body.author)
        upd = {"draft": None, "draft_status": None}
        if not c.get("first_response_at"):
            upd["first_response_at"] = db.now()
        db.update_conversation(cid, **upd)
        db.add_event(cid, "reply_sent", {"by": body.author, "message_id": mid,
                                         "used_ai_draft": ai_text is not None, "edited": ai_text is not None and ai_text.strip() != body.text.strip()})
        return {"conversation": db.get_conversation(cid), "message_id": mid, **meta(t0, "agent")}

    @app.post("/api/conversations/{cid}/regenerate")
    def regenerate(cid: str, wait: bool = True):
        t0 = time.perf_counter()
        _get(cid)
        d = pipeline.request_draft(cid, wait=wait)
        g = get_generator()
        return {"draft": d, "conversation": db.get_conversation(cid), **meta(t0, g.name, g.model)}

    @app.post("/api/conversations/{cid}/escalate")
    def escalate(cid: str, body: EscalateIn = EscalateIn()):
        t0 = time.perf_counter()
        _get(cid)
        pipeline.escalate(cid, [body.reason], actor="agent")
        return {"conversation": db.get_conversation(cid), **meta(t0, "agent")}

    @app.post("/api/conversations/{cid}/resolve")
    def resolve(cid: str):
        t0 = time.perf_counter()
        _get(cid)
        t = db.now()
        db.update_conversation(cid, status="resolved", resolved_at=t, last_resolved_at=t)
        db.add_event(cid, "resolved", {})
        return {"conversation": db.get_conversation(cid), **meta(t0, "agent")}

    @app.post("/api/conversations/{cid}/assign")
    def assign(cid: str, body: AssignIn):
        t0 = time.perf_counter()
        _get(cid)
        db.update_conversation(cid, assignee=body.assignee)
        db.add_event(cid, "assigned", {"assignee": body.assignee})
        return {"conversation": db.get_conversation(cid), **meta(t0, "agent")}

    @app.get("/api/analytics")
    def analytics(days: int = 14):
        t0 = time.perf_counter()
        convs = db.list_conversations(limit=100000)
        msgs = db.all_messages()
        evs = db.events()
        cust = [m for m in msgs if m["role"] == "customer"]
        today = datetime.now(timezone.utc).date()
        by_day = Counter(m["created_at"][:10] for m in cust)
        volume = [{"date": (today - timedelta(days=i)).isoformat(), "count": by_day.get((today - timedelta(days=i)).isoformat(), 0)}
                  for i in range(days - 1, -1, -1)]
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        by_hour = Counter(m["created_at"][:13] for m in cust if m["created_at"] >= cutoff)
        hours = []
        for i in range(23, -1, -1):
            h = (datetime.now(timezone.utc) - timedelta(hours=i)).strftime("%Y-%m-%dT%H")
            hours.append({"hour": h + ":00Z", "count": by_hour.get(h, 0)})
        n = len(convs)
        escalated_ids = {e["conversation_id"] for e in evs if e["kind"] == "escalated"}
        frts = []
        for c in convs:
            if c.get("first_response_at"):
                frts.append((datetime.fromisoformat(c["first_response_at"]) - datetime.fromisoformat(c["created_at"])).total_seconds())
        analysed = [m["analysis"] for m in cust if m.get("analysis")]
        frustrated = sum(1 for a in analysed if a["classification"]["frustrated"] >= 0.5)
        lat = [a["latency_ms"] for a in analysed if a["source"] == "subq"]
        dl = [e["detail"]["latency_ms"] for e in evs if e["kind"] == "draft" and e.get("detail")]
        return {
            "totals": {"conversations": n, "messages": len(cust),
                       "open": sum(c["status"] == "open" for c in convs),
                       "escalated": sum(c["status"] == "escalated" for c in convs),
                       "resolved": sum(c["status"] == "resolved" for c in convs)},
            "volume_by_day": volume, "volume_by_hour": hours,
            "category_distribution": dict(Counter(c["category"] or "unknown" for c in convs)),
            "department_distribution": dict(Counter(c["department"] or "unknown" for c in convs)),
            "priority_levels": {p: sum(c["priority_label"] == p for c in convs) for p in ("low", "medium", "high", "urgent")},
            "escalation_rate": round(len(escalated_ids) / n, 4) if n else 0,
            "resolution_rate": round(sum(c["status"] == "resolved" for c in convs) / n, 4) if n else 0,
            "avg_first_response_seconds": round(sum(frts) / len(frts), 1) if frts else None,
            "frustration_rate": round(frustrated / len(analysed), 4) if analysed else 0,
            "classification_sources": dict(Counter(a["source"] for a in analysed)),
            "avg_classification_latency_ms": int(sum(lat) / len(lat)) if lat else None,
            "avg_draft_latency_ms": int(sum(dl) / len(dl)) if dl else None,
            "draft_status": dict(Counter((c.get("draft_status") or "none") for c in convs)),
            **meta(t0, "db"),
        }

    @app.get("/api/settings")
    def get_settings():
        t0 = time.perf_counter()
        return {"settings": db.get_settings(), "defaults": config.DEFAULT_SETTINGS, **meta(t0, "db")}

    @app.put("/api/settings")
    def put_settings(body: SettingsIn):
        t0 = time.perf_counter()
        vals = {k: v for k, v in body.model_dump().items() if v is not None}
        db.put_settings(vals)
        return {"settings": db.get_settings(), "updated": sorted(vals), **meta(t0, "db")}

    @app.post("/api/seed")
    def seed():
        t0 = time.perf_counter()
        return {"seed": samples.start(), **meta(t0, "pipeline")}

    @app.get("/api/seed")
    def seed_status():
        t0 = time.perf_counter()
        return {"seed": samples.status(), "draft_queue": pipeline.queue_depth(), **meta(t0, "pipeline")}

    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    return app
