"""End-to-end WhatsApp bot simulation — the Sarah / Peter / Luna story,
played entirely through the real webhook with Cloud API-shaped payloads.

Sarah onboards with the bot, registers Peter once (contact card), sends
her chat export (confession + acceptance inside -> together, baseline
built). Luna onboards, registers the same Peter, sends her export
(friendship baseline incl. a normal 'ily', then a late-night spike after
Sarah's relationship started). Sarah gets the match notice, then the
pattern-change flag with review buttons, taps 'Keep alerts on', and the
review gate opens. A forwarded message and a re-sent export (dedupe)
close the loop.

Run:  python scripts/simulate_whatsapp.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["OVERLAP_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="overlap-wa-sim-"), "sim.db"
)

from fastapi.testclient import TestClient  # noqa: E402

from app import store, whatsapp as wa  # noqa: E402
from app.main import app  # noqa: E402
from app.normalize import normalize_identifier  # noqa: E402

FIXTURES = {
    "media-sarah-export": open(
        os.path.join(os.path.dirname(__file__), "fixtures", "sarah_peter.txt"), "rb"
    ).read(),
    "media-luna-export": open(
        os.path.join(os.path.dirname(__file__), "fixtures", "luna_peter.txt"), "rb"
    ).read(),
}
wa.fetch_media_bytes = lambda media_id: FIXTURES[media_id]  # noqa: E731

client = TestClient(app)
SARAH = "15551110001"
LUNA = "15551110002"
PETER_NUMBER = "15550107777"
PETER_HASH = normalize_identifier(PETER_NUMBER)["hash"]
MATCH_ID = f"match_{PETER_HASH[:16]}"
_counter = [0]
_seen = [0]


def payload(wa_id, name, message):
    _counter[0] += 1
    message.setdefault("id", f"wamid.sim{_counter[0]}")
    message.setdefault("from", wa_id)
    message.setdefault("timestamp", str(1767225600 + _counter[0] * 60))
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [
                                {"wa_id": wa_id, "profile": {"name": name}}
                            ],
                            "messages": [message],
                        }
                    }
                ]
            }
        ],
    }


def send(wa_id, name, message, note):
    r = client.post("/webhooks/whatsapp", json=payload(wa_id, name, message))
    assert r.status_code == 200, r.text
    print(f"\n--- {note}")
    for record in wa.DRY_RUN_LOG[_seen[0]:]:
        who = "Sarah" if record["to"] == SARAH else "Luna"
        print(f"  OVERLAP → {who}: {record['text']}")
        if record["buttons"]:
            print(f"    buttons: {[b['title'] for b in record['buttons']]}")
    _seen[0] = len(wa.DRY_RUN_LOG)


def text(body, forwarded=False):
    msg = {"type": "text", "text": {"body": body}}
    if forwarded:
        msg["context"] = {"forwarded": True}
    return msg


def contact_card(phone, wa_id=None):
    phones = [{"phone": phone}]
    if wa_id:
        phones[0]["wa_id"] = wa_id
    return {
        "type": "contacts",
        "contacts": [{"name": {"formatted_name": "Peter"}, "phones": phones}],
    }


def document(media_id, filename):
    return {
        "type": "document",
        "document": {"id": media_id, "filename": filename, "mime_type": "text/plain"},
    }


def button(button_id):
    return {
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": "tap"}},
    }


def bot_said(to_wa, needle):
    return any(
        rec["to"] == to_wa and needle in rec["text"] for rec in wa.DRY_RUN_LOG
    )


print("=== OVERLAP WhatsApp bot — end-to-end simulation ===")

send(SARAH, "Sarah", text("hi"), "Sarah says hi (not onboarded yet)")
assert bot_said(SARAH, "Reply YES"), "onboarding prompt missing"

send(SARAH, "Sarah", text("YES"), "Sarah turns OVERLAP on")
assert bot_said(SARAH, "You're in")

send(SARAH, "Sarah", contact_card("+1 555-010-7777", PETER_NUMBER),
     "Sarah shares Peter's contact card (once per person, ever)")
assert bot_said(SARAH, "Saved Peter")

send(SARAH, "Sarah", document("media-sarah-export", "WhatsApp Chat with Peter.txt"),
     "Sarah sends her chat export with Peter")
assert bot_said(SARAH, "Imported your chat with Peter"), "export import failed"
assert bot_said(SARAH, "together since"), "relationship not detected from export"

send(LUNA, "Luna", text("hi"), "Luna says hi")
send(LUNA, "Luna", text("YES"), "Luna turns OVERLAP on")
send(LUNA, "Luna", contact_card("5550107777"),
     "Luna shares Peter's contact card (same number, other format)")

send(LUNA, "Luna", document("media-luna-export", "WhatsApp Chat with Peter.txt"),
     "Luna sends her export (baseline friendship + late-night spike)")
assert bot_said(LUNA, "Imported your chat with Peter")
# A match is about submitted messages, so the notices fire on Luna's first
# signals (her export) — not on registration alone.
assert bot_said(LUNA, "Potential overlap"), "Luna should see the match notice"
assert bot_said(SARAH, "Potential overlap"), "Sarah should get the outbox notice"
assert bot_said(SARAH, "Pattern change for Peter"), "Sarah should get the flag"
flag_text = next(
    rec["text"] for rec in wa.DRY_RUN_LOG
    if rec["to"] == SARAH and "Pattern change" in rec["text"]
)
assert "Frequency jumped" in flag_text and "Late-night share rose" in flag_text
assert "Affectionate language is new" not in flag_text, \
    "baseline 'ily' must not drive the flag"

send(SARAH, "Sarah", button(f"review:{PETER_HASH[:16]}:confirmed"),
     "Sarah taps 'Keep alerts on'")
assert bot_said(SARAH, "Alerts on for this match")

sarah_owner = store.owner_id_for_address("whatsapp", SARAH)
match = client.get(f"/matches/{MATCH_ID}", params={"owner_id": sarah_owner}).json()
assert match["your_review"] == "confirmed", match
alert = client.post(f"/matches/{MATCH_ID}/alert", json={"owner_id": sarah_owner})
assert alert.status_code == 201, alert.text
print("\n--- Review gate verified through the API: confirmed review, alert recorded.")

send(SARAH, "Sarah", text("i loved last night", forwarded=True),
     "Sarah forwards a fresh message from Peter")
assert bot_said(SARAH, "✓ Logged 1 message for Peter"), "forward not attributed"

before = len(wa.DRY_RUN_LOG)
send(SARAH, "Sarah", document("media-sarah-export", "WhatsApp Chat with Peter.txt"),
     "Sarah re-sends the same export")
assert bot_said(SARAH, "0 new messages"), "dedupe failed"

send(SARAH, "Sarah", text("status"), "Sarah asks for status")
assert bot_said(SARAH, "together since 2026-10-03")

print("\n=== ALL CHECKS PASSED: WhatsApp bot works end to end ===")
