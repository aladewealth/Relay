"""Sample inbox. Each message is sent through the REAL pipeline (no precomputed results)."""
import threading
import time

from . import pipeline

# (customer_name, email, channel, text, thread_key) - messages sharing a thread_key go to one conversation
SAMPLES = [
    ("Amara Okafor", "amara.okafor@gmail.com", "email",
     "Hi, I was charged twice for my Lumen Plus annual plan this morning - two payments of $99.00 on my card ending 4421 (invoice INV-20931). Can you please fix this and refund the duplicate?", None),
    ("Daniel Mensah", "d.mensah@outlook.com", "chat",
     "My Lumen Cam Pro stopped showing night vision after last night's update. Firmware says 4.2.0. Daytime video works fine. Is there a fix?", None),
    ("Sofia Rossi", "sofia.rossi@proton.me", "email",
     "I can't log in to my account. I reset my password twice but the link says it has expired every time. I need to check my cameras while I'm travelling.", None),
    ("Kunle Adeyemi", "kunle@adeyemi-logistics.ng", "email",
     "This is the THIRD time I'm writing. Order #A-4471 (Lumen Hub + 3 door sensors, $246) still hasn't arrived after 3 weeks and tracking hasn't moved since Oct 24. If this isn't sorted by Friday I'm filing a chargeback with my bank and talking to a lawyer.", None),
    ("Emily Carter", "emily.carter@yahoo.com", "web_form",
     "Hello! Does the Lumen Door Sensor work with Apple HomeKit, and is there a discount if I buy 5?", None),
    ("Tunde Bakare", "tunde.b@gmail.com", "chat",
     "I want to cancel my Lumen Plus subscription. I don't use the cloud recordings anymore.", None),
    ("Grace Liu", "grace.liu@company.io", "email",
     "Someone changed the email on my Lumen account and I got a 2FA code I never requested. I think my account was hacked - please lock it immediately.", None),
    ("Marco Diaz", "marco.diaz@gmail.com", "email",
     "The Lumen Cam Pro I received (order ORD-58812) has a cracked lens. I'd like to return it for a refund of $199.", None),
    ("Priya Nair", "priya.nair@outlook.com", "chat",
     "Honestly your support is the worst. I waited 2 days for a reply and the agent just copy-pasted a help article that didn't answer anything.", None),
    ("Jonas Weber", "jonas.weber@web.de", "email",
     "I'd like to upgrade from the monthly Lumen Plus plan to the annual one. Will I be charged the full $99 today?", None),
    ("Kunle Adeyemi", "kunle@adeyemi-logistics.ng", "email",
     "Still nothing on #A-4471. Nobody has replied. This is unacceptable.", "kunle"),
    ("Kunle Adeyemi", "kunle@adeyemi-logistics.ng", "email",
     "Hello?? Do I need to come to your office? Give me a tracking update or my money back today.", "kunle"),
]

_state = {"running": False, "done": 0, "total": len(SAMPLES), "errors": [], "started_at": None, "finished_at": None,
          "conversation_ids": []}
_lock = threading.Lock()


def status():
    return dict(_state)


def _run():
    threads = {}
    for name, email, channel, text, key in SAMPLES:
        try:
            # Kunle's follow-ups continue his original conversation (index 3)
            cid = threads.get(key) if key else None
            r = pipeline.process_message(name, email, channel, text, conversation_id=cid, wait_draft=False)
            if name == "Kunle Adeyemi" and "kunle" not in threads:
                threads["kunle"] = r["conversation_id"]
            _state["conversation_ids"].append(r["conversation_id"])
        except Exception as e:  # noqa: BLE001
            _state["errors"].append(f"{name}: {type(e).__name__}: {e}")
        _state["done"] += 1
    _state["running"] = False
    _state["finished_at"] = time.time()


def start():
    with _lock:
        if _state["running"]:
            return status()
        _state.update(running=True, done=0, errors=[], started_at=time.time(), finished_at=None, conversation_ids=[])
    threading.Thread(target=_run, daemon=True).start()
    return status()
