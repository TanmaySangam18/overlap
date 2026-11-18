import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import store, telegram as tg
from app.main import app
from app.normalize import normalize_identifier
from scripts.telegram_client import build_payload, resolve_sender_identifier

client = TestClient(app)
FIXTURES = Path(__file__).resolve().parent.parent / "scripts" / "fixtures"

SARAH_CHAT = 111111
LUNA_CHAT = 222222
PETER_WA = "15550107777"
PETER_HASH = normalize_identifier(PETER_WA)["hash"]
PETER_TG_ID = 987654321
PETER_TG_HASH = normalize_identifier(f"telegram:{PETER_TG_ID}")["hash"]


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    os.environ["OVERLAP_DB_PATH"] = str(tmp_path / "tg-test.db")
    os.environ.pop("TELEGRAM_WEBHOOK_SECRET", None)
    tg.DRY_RUN_LOG.clear()
    yield


@pytest.fixture
def media(monkeypatch):
    files = {
        "file-sarah": (FIXTURES / "sarah_peter.txt").read_bytes(),
        "file-luna": (FIXTURES / "luna_peter.txt").read_bytes(),
    }
    monkeypatch.setattr(tg, "fetch_document_bytes", lambda fid: files[fid])
    return files


_counter = [5000]


def update(chat_id, first_name, message):
    _counter[0] += 1
    message.setdefault("message_id", _counter[0])
    message.setdefault("date", 1767225600 + _counter[0])
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
            "message": {
                "message_id": 1,
                "date": 1767225600,
                "chat": {"id": chat_id, "type": "private"},
            },
            "data": data,
        },
    }


def send(payload):
    r = client.post("/webhooks/telegram", json=payload)
    assert r.status_code == 200, r.text
    return r


def text(body, forwarded=False):
    msg = {"text": body}
    if forwarded:
        msg["forward_origin"] = {
            "type": "user",
            "date": 1767225600,
            "sender_user": {"id": PETER_TG_ID, "first_name": "Peter"},
        }
    return msg


def card(phone, tg_id=None):
    contact = {"phone_number": phone, "first_name": "Peter"}
    if tg_id is not None:
        contact["user_id"] = tg_id
    return {"contact": contact}


def doc(file_id):
    return {"document": {"file_id": file_id, "file_name": "chat.txt",
                         "mime_type": "text/plain"}}


def onboard(chat_id, name):
    send(update(chat_id, name, text("hi")))
    send(update(chat_id, name, text("YES")))


def said(chat_id, needle):
    return any(
        needle in rec["text"] for rec in tg.DRY_RUN_LOG if rec["to"] == str(chat_id)
    )


# ---------------------------------------------------------------------------
# Identifier: Telegram user id
# ---------------------------------------------------------------------------


def test_normalize_telegram_id():
    ident = normalize_identifier("telegram:987654321")
    assert ident["type"] == "telegram"
    assert ident["canonical"] == "telegram:987654321"
    assert ident["masked"] == "tg:***4321"
    # Same id, different prefix spelling, same hash.
    assert normalize_identifier("tg:987654321")["hash"] == ident["hash"]
    with pytest.raises(ValueError):
        normalize_identifier("telegram:")
    with pytest.raises(ValueError):
        normalize_identifier("telegram:abc")


def test_client_resolves_phone_first_then_telegram_id():
    assert resolve_sender_identifier({"phone": "+15550107777", "id": 1}) == "+15550107777"
    assert resolve_sender_identifier({"phone": "", "id": PETER_TG_ID}) == f"telegram:{PETER_TG_ID}"
    assert resolve_sender_identifier({"phone": None, "id": None}) is None
    assert resolve_sender_identifier({}) is None


def test_client_build_payload_skips_empty_and_sent_by_default():
    base = {
        "sender": {"phone": "+15550107777", "id": 1},
        "text": "hey",
        "sent_at": datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc),
        "is_from_me": False,
        "is_group": False,
        "partner": None,
        "include_sent": False,
    }
    payload, reason = build_payload(base)
    assert reason is None
    assert payload["sender_identifier"] == "+15550107777"
    assert payload["conversation_id"] == "telegram-client"

    empty, _ = build_payload({**base, "text": "   "})
    assert empty is None

    sent, _ = build_payload({**base, "is_from_me": True})
    assert sent is None  # sent needs the opt-in

    sent_in, _ = build_payload({
        **base, "is_from_me": True, "include_sent": True,
        "partner": {"phone": "+15550107777", "id": 1},
    })
    assert sent_in["conversation_id"] == "telegram-client-sent"

    sent_group, _ = build_payload({
        **base, "is_from_me": True, "include_sent": True, "is_group": True,
        "partner": {"phone": "+15550107777", "id": 1},
    })
    assert sent_group is None


# ---------------------------------------------------------------------------
# Bot webhook
# ---------------------------------------------------------------------------


def test_webhook_secret_enforced_when_set(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "tg-secret-123")
    bad = client.post("/webhooks/telegram", json=update(SARAH_CHAT, "Sarah", text("hi")))
    assert bad.status_code == 403
    good = client.post(
        "/webhooks/telegram",
        json=update(SARAH_CHAT, "Sarah", text("hi")),
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret-123"},
    )
    assert good.status_code == 200


def test_consent_gate_and_telegram_wording():
    send(update(SARAH_CHAT, "Sarah", card("+1 555-010-7777", PETER_TG_ID)))
    owner = store.owner_id_for_address("telegram", str(SARAH_CHAT))
    assert store.persons_for_owner(owner) == []
    assert said(SARAH_CHAT, "Reply YES")
    # The intro names Telegram, not WhatsApp, inside a Telegram chat.
    assert said(SARAH_CHAT, "Telegram bot can't see them")
    assert not any("WhatsApp" in rec["text"] for rec in tg.DRY_RUN_LOG
                   if rec["to"] == str(SARAH_CHAT))


def test_once_per_person_via_telegram_contact():
    onboard(SARAH_CHAT, "Sarah")
    send(update(SARAH_CHAT, "Sarah", card("+1 555-010-7777", PETER_TG_ID)))
    assert said(SARAH_CHAT, "Saved Peter")
    tg.DRY_RUN_LOG.clear()
    send(update(SARAH_CHAT, "Sarah", card("5550107777")))
    assert said(SARAH_CHAT, "already saved")
    owner = store.owner_id_for_address("telegram", str(SARAH_CHAT))
    assert len(store.persons_for_owner(owner)) == 1


def test_contact_without_phone_falls_back_to_telegram_id():
    onboard(SARAH_CHAT, "Sarah")
    contact = {"contact": {"first_name": "Peter", "user_id": PETER_TG_ID}}
    send(update(SARAH_CHAT, "Sarah", contact))
    assert said(SARAH_CHAT, "Saved Peter")
    owner = store.owner_id_for_address("telegram", str(SARAH_CHAT))
    persons = store.persons_for_owner(owner)
    assert len(persons) == 1
    assert persons[0]["identifier_type"] == "telegram"


def test_forward_attribution_single_person():
    onboard(SARAH_CHAT, "Sarah")
    send(update(SARAH_CHAT, "Sarah", card("+1 555-010-7777", PETER_TG_ID)))
    tg.DRY_RUN_LOG.clear()
    send(update(SARAH_CHAT, "Sarah", text("i loved last night", forwarded=True)))
    assert said(SARAH_CHAT, "✓ Logged 1 message for Peter")


def test_unsupported_document_gets_plain_answer():
    onboard(SARAH_CHAT, "Sarah")
    send(update(SARAH_CHAT, "Sarah", {
        "document": {"file_id": "x", "file_name": "photo.jpg",
                     "mime_type": "image/jpeg"}}))
    assert said(SARAH_CHAT, "different format")


# ---------------------------------------------------------------------------
# Full story through the Telegram bot
# ---------------------------------------------------------------------------


def test_full_story_match_flag_review(media):
    onboard(SARAH_CHAT, "Sarah")
    send(update(SARAH_CHAT, "Sarah", card("+1 555-010-7777", PETER_TG_ID)))
    send(update(SARAH_CHAT, "Sarah", doc("file-sarah")))
    assert said(SARAH_CHAT, "together since")
    sarah_owner = store.owner_id_for_address("telegram", str(SARAH_CHAT))
    pair = store.get_pair_state(sarah_owner, PETER_HASH)
    assert pair["relationship_state"] == "together"
    assert pair["since_iso"].startswith("2026-10-03")

    onboard(LUNA_CHAT, "Luna")
    send(update(LUNA_CHAT, "Luna", card("5550107777")))
    send(update(LUNA_CHAT, "Luna", doc("file-luna")))
    assert said(LUNA_CHAT, "Potential overlap")
    assert said(SARAH_CHAT, "Pattern change for Peter")

    match_id = f"match_{PETER_HASH[:16]}"
    blocked = client.post(f"/matches/{match_id}/alert", json={"owner_id": sarah_owner})
    assert blocked.status_code == 409

    # Sarah taps "Keep alerts on" — a Telegram callback query.
    send(callback(SARAH_CHAT, "Sarah", f"review:{PETER_HASH[:16]}:confirmed"))
    assert said(SARAH_CHAT, "Alerts on for this match")
    got = client.get(f"/matches/{match_id}", params={"owner_id": sarah_owner})
    assert got.json()["your_review"] == "confirmed"


# ---------------------------------------------------------------------------
# /ingest/telegram — the automatic user-client endpoint
# ---------------------------------------------------------------------------


def test_ingest_telegram_by_phone_and_by_telegram_id():
    token_a = client.post("/tokens", json={"owner_id": "tg-sarah"}).json()["ingest_token"]
    token_b = client.post("/tokens", json={"owner_id": "tg-luna"}).json()["ingest_token"]

    r = client.post(
        "/ingest/telegram",
        json={"sender_identifier": "+1 555-010-7777", "message_text": "hey",
              "sent_at": "2026-11-17T12:00:00Z"},
        headers={"X-Overlap-Token": token_a},
    )
    assert r.status_code == 201, r.text
    assert r.json()["ingested_via"] == "telegram_user_client"
    assert r.json()["potential_match"] is None

    # Same person, seen by the client as a Telegram id (phone hidden).
    r2 = client.post(
        "/ingest/telegram",
        json={"sender_identifier": f"telegram:{PETER_TG_ID}",
              "message_text": "hey again", "sent_at": "2026-11-17T12:05:00Z"},
        headers={"X-Overlap-Token": token_b},
    )
    assert r2.status_code == 201, r2.text
    # Different identifier forms (phone vs telegram id) do NOT match —
    # the client docstring says so plainly. The match that does fire
    # is two clients seeing the same Telegram id.
    assert r2.json()["potential_match"] is None

    r3 = client.post(
        "/ingest/telegram",
        json={"sender_identifier": f"telegram:{PETER_TG_ID}",
              "message_text": "hey from sarah too",
              "sent_at": "2026-11-17T12:10:00Z"},
        headers={"X-Overlap-Token": token_a},
    )
    assert r3.json()["potential_match"] is not None
    assert r3.json()["potential_match"]["distinct_owner_count"] == 2

    bad = client.post(
        "/ingest/telegram",
        json={"sender_identifier": "+1 555-010-7777", "message_text": "hey"},
        headers={"X-Overlap-Token": "nope"},
    )
    assert bad.status_code == 401
