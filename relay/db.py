"""SQLite persistence."""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from . import config

_lock = threading.RLock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, customer_name TEXT, email TEXT, channel TEXT, subject TEXT,
  status TEXT NOT NULL DEFAULT 'open', assignee TEXT, department TEXT, category TEXT,
  priority_label TEXT, priority_score REAL, frustrated REAL,
  escalated INTEGER NOT NULL DEFAULT 0, escalation_reasons TEXT,
  routing TEXT, draft TEXT, draft_status TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, first_response_at TEXT, resolved_at TEXT, last_resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
  text TEXT NOT NULL, created_at TEXT NOT NULL, analysis TEXT, author TEXT,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id)
);
CREATE INDEX IF NOT EXISTS ix_msg_conv ON messages(conversation_id);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, kind TEXT NOT NULL,
  detail TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_JSON_COLS = ("escalation_reasons", "routing", "draft")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    c = sqlite3.connect(config.db_path(), check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, connect() as c:
        c.executescript(_SCHEMA)


def _conv(row):
    if row is None:
        return None
    d = dict(row)
    for k in _JSON_COLS:
        d[k] = json.loads(d[k]) if d.get(k) else None
    d["escalated"] = bool(d["escalated"])
    return d


def _msg(row):
    d = dict(row)
    d["analysis"] = json.loads(d["analysis"]) if d.get("analysis") else None
    return d


def create_conversation(customer_name, email, channel, subject):
    cid = "c_" + uuid.uuid4().hex[:10]
    t = now()
    with _lock, connect() as c:
        c.execute("INSERT INTO conversations(id,customer_name,email,channel,subject,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                  (cid, customer_name, email, channel, subject, "open", t, t))
    return cid


def get_conversation(cid):
    with connect() as c:
        return _conv(c.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone())


def list_conversations(status=None, department=None, escalated=None, limit=500):
    q, args = "SELECT * FROM conversations WHERE 1=1", []
    if status:
        q += " AND status=?"; args.append(status)
    if department:
        q += " AND department=?"; args.append(department)
    if escalated is not None:
        q += " AND escalated=?"; args.append(1 if escalated else 0)
    q += " ORDER BY updated_at DESC LIMIT ?"; args.append(limit)
    with connect() as c:
        return [_conv(r) for r in c.execute(q, args).fetchall()]


def update_conversation(cid, **fields):
    if not fields:
        return
    for k in _JSON_COLS:
        if k in fields and fields[k] is not None:
            fields[k] = json.dumps(fields[k])
    if "escalated" in fields:
        fields["escalated"] = 1 if fields["escalated"] else 0
    fields["updated_at"] = now()
    sets = ", ".join(f"{k}=?" for k in fields)
    with _lock, connect() as c:
        c.execute(f"UPDATE conversations SET {sets} WHERE id=?", (*fields.values(), cid))


def add_message(cid, role, text, analysis=None, author=None):
    with _lock, connect() as c:
        cur = c.execute("INSERT INTO messages(conversation_id,role,text,created_at,analysis,author) VALUES(?,?,?,?,?,?)",
                        (cid, role, text, now(), json.dumps(analysis) if analysis else None, author))
        return cur.lastrowid


def set_message_analysis(mid, analysis):
    with _lock, connect() as c:
        c.execute("UPDATE messages SET analysis=? WHERE id=?", (json.dumps(analysis), mid))


def messages(cid):
    with connect() as c:
        return [_msg(r) for r in c.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (cid,)).fetchall()]


def all_messages():
    with connect() as c:
        return [_msg(r) for r in c.execute("SELECT * FROM messages ORDER BY id").fetchall()]


def add_event(cid, kind, detail=None):
    with _lock, connect() as c:
        c.execute("INSERT INTO events(conversation_id,kind,detail,created_at) VALUES(?,?,?,?)",
                  (cid, kind, json.dumps(detail) if detail is not None else None, now()))


def events(cid=None):
    with connect() as c:
        rows = c.execute("SELECT * FROM events" + (" WHERE conversation_id=?" if cid else "") + " ORDER BY id",
                         (cid,) if cid else ()).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows]


def get_settings():
    s = dict(config.DEFAULT_SETTINGS)
    with connect() as c:
        for r in c.execute("SELECT key,value FROM settings").fetchall():
            s[r["key"]] = json.loads(r["value"])
    return s


def put_settings(values):
    with _lock, connect() as c:
        for k, v in values.items():
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (k, json.dumps(v)))
