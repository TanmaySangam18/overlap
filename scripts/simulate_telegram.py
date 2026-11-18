"""End-to-end Telegram bot simulation — the Sarah / Peter / Luna story,
played entirely through the real webhook with Bot API-shaped updates.

Same story as scripts/simulate_whatsapp.py, on the Telegram bot:
Sarah onboards, registers Peter once (contact card), sends her chat
export (confession + acceptance inside -> together, baseline built).
Luna onboards, registers the same Peter, sends her export (friendship
baseline incl. a normal 'ily', then a late-night spike after Sarah's
relationship started). Sarah gets the match notice, then the
pattern-change flag with review buttons, taps 'Keep alerts on' (a
Telegram callback query), and the review gate opens. A forwarded
message and a re-sent export (dedupe) close the loop.

Run:  python scripts/simulate_telegram.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["OVERLAP_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="overlap-tg-sim-"), "sim.db"
)

from fastapi.testclient import TestClient  # noqa: E402

from app import store, telegram as tg  # noqa: E402
from app.main import app  # noqa: E402
from app.normalize import normalize_identifier  # noqa: E402

FIXTURES = {
    "file-sarah": open(
        os.path.join(os.path.dirname(__file__), "fixtures", "sarah_peter.txt"), "rb"
    ).read(),
    "file-luna": open(
        os.path.join(os.path.dirname(__file__), "fixtures", "luna_peter.txt"), "rb"
    ).read(),
}
tg.fetch_document_bytes = lambda file_id: FIXTURES[file_id]  # noqa: E731

client = TestClient(app)
SARAH = 111111
LUNA = 222222
PETER_NUMBER = "15550107777"
PETER_HASH = normalize_identifier(PETER_NUMBER)["hash"]
MATCH_ID = f"match_{PETER_HASH[:16]}"
_counter = [0]
_seen = [0]


def update(chat_id, first_name, message):
    _counter[0] += 1
    message.setdefault("message_id", _counter[0])
    message.setdefault("date", 1767225600 + _counter[0] * 60)
    message.setdefault("chat", {"id": chat_id, "type": "private"})
    message.setdefault("from", {"id": chat_id, "first_name": first_name})
    return {"update_id": _counter[0], "message": message}


def callback(chat_id, first_name, data):
    _counter[0] += 1
    return {
        "update_id": _counter[0],
        "callback_query": {
            "id": f"cb{_counter[0]}",
            "from": {"id": chat_id, "first_name": first_name},
            "message": {"message_id": 1, "date": 1767225600,
                        "chat": {"id": chat_id, "type": "private"}},
            "data": data,
        },
    }


def send(payload, note):
    r = client.post("/webhooks/telegram", json=payload)
    assert r.status_code == 200, r.text
    print(f"\n--- {note}")
    for record in tg.DRY_RUN_LOG[_seen[0]:]:
        who = "Sarah" if record["to"] == str(SARAH) else "Luna"
        print(f"  OVERLAP → {who}: {record['text']}")
        if record["buttons"]:
            print(f"    buttons: {[b['title'] for b in record['buttons']]}")
    _seen[0] = len(tg.DRY_RUN_LOG)


def text(body, forwarded=False):
    msg = {"text": body}
    if forwarded:
        msg["forward_origin"] = {
            "type": "user", "date": 1767225600,
            "sender_user": {"id": 987654321, "first_name": "Peter"},
        }
    return msg


def contact_card(phone):
    return {"contact": {"phone_number": phone, "first_name": "Peter"}}


def document(file_id):
    return {"document": {"file_id": file_id, "file_name": "chat.txt",
                         "mime_type": "text/plain"}}


def bot_said(to_chat, needle):
    return any(
        rec["to"] == str(to_chat) and needle in rec["text"] for rec in tg.DRY_RUN_LOG
    )


print("=== OVERLAP Telegram bot — end-to-end simulation ===")

send(update(SARAH, "Sarah", text("hi")), "Sarah says hi (not onboarded yet)")
assert bot_said(SARAH, "Reply YES"), "onboarding prompt missing"

send(update(SARAH, "Sarah", text("YES")), "Sarah turns OVERLAP on")
assert bot_said(SARAH, "You're in")

send(update(SARAH, "Sarah", contact_card("+1 555-010-7777")),
     "Sarah shares Peter's contact card (once per person, ever)")
assert bot_said(SARAH, "Saved Peter")

send(update(SARAH, "Sarah", document("file-sarah")),
     "Sarah sends her chat export with Peter")
assert bot_said(SARAH, "Imported your chat with Peter"), "export import failed"
assert bot_said(SARAH, "together since"), "relationship not detected from export"

send(update(LUNA, "Luna", text("hi")), "Luna says hi")
send(update(LUNA, "Luna", text("YES")), "Luna turns OVERLAP on")
send(update(LUNA, "Luna", contact_card("5550107777")),
     "Luna shares Peter's contact card (same number, other format)")

send(update(LUNA, "Luna", document("file-luna")),
     "Luna sends her export (baseline friendship + late-night spike)")
assert bot_said(LUNA, "Imported your chat with Peter")
assert bot_said(LUNA, "Potential overlap"), "Luna should see the match notice"
assert bot_said(SARAH, "Potential overlap"), "Sarah should get the outbox notice"
assert bot_said(SARAH, "Pattern change for Peter"), "Sarah should get the flag"
flag_text = next(
    rec["text"] for rec in tg.DRY_RUN_LOG
    if rec["to"] == str(SARAH) and "Pattern change" in rec["text"]
)
assert "Frequency jumped" in flag_text and "Late-night share rose" in flag_text
assert "Affectionate language is new" not in flag_text, \
    "baseline 'ily' must not drive the flag"

send(callback(SARAH, "Sarah", f"review:{PETER_HASH[:16]}:confirmed"),
     "Sarah taps 'Keep alerts on' (Telegram callback query)")
assert bot_said(SARAH, "Alerts on for this match")

sarah_owner = store.owner_id_for_address("telegram", str(SARAH))
sarah_headers = {"X-Overlap-Token": store.create_ingest_token(sarah_owner)}
match = client.get(f"/matches/{MATCH_ID}", params={"owner_id": sarah_owner},
                   headers=sarah_headers).json()
assert match["your_review"] == "confirmed", match
alert = client.post(f"/matches/{MATCH_ID}/alert", json={"owner_id": sarah_owner},
                    headers=sarah_headers)
assert alert.status_code == 201, alert.text
print("\n--- Review gate verified through the API: confirmed review, alert recorded.")

send(update(SARAH, "Sarah", text("i loved last night", forwarded=True)),
     "Sarah forwards a fresh message from Peter (forward_origin preserved)")
assert bot_said(SARAH, "✓ Logged 1 message for Peter"), "forward not attributed"

send(update(SARAH, "Sarah", document("file-sarah")),
     "Sarah re-sends the same export")
assert bot_said(SARAH, "0 new messages"), "dedupe failed"

send(update(SARAH, "Sarah", text("status")), "Sarah asks for status")
assert bot_said(SARAH, "together since 2026-10-03")

print("\n=== ALL CHECKS PASSED: Telegram bot works end to end ===")
