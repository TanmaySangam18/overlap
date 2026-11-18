"""Agent-quality upgrades benchmarked against the leading chat agents
(Series, Yuki, Poke, eluv): loose natural phrasing instead of exact
string matching, an activity log, undo, pause/resume — and the
reliability fixes underneath (dedupe released on failure, outbox
notices survive send failures, multi-card messages, no silence on
unsupported message types).
"""

import os

import pytest
from fastapi.testclient import TestClient

from app import evidence, store, whatsapp as wa
from app.main import app
from app.normalize import normalize_identifier

client = TestClient(app)
client_no_raise = TestClient(app, raise_server_exceptions=False)

SARAH = "15551110001"
LUNA = "15551110002"
PETER_WA = "15550107777"
PETER_HASH = normalize_identifier(PETER_WA)["hash"]

PASTE = (
    "[18/11/2026, 9:41 PM] Peter: are you up\n"
    "[18/11/2026, 9:42 PM] Peter: come over"
)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    os.environ["OVERLAP_DB_PATH"] = str(tmp_path / "quality-test.db")
    wa.DRY_RUN_LOG.clear()
    yield


_counter = [9000]


def payload(wa_id, name, message):
    _counter[0] += 1
    message.setdefault("id", f"wamid.q{_counter[0]}")
    message.setdefault("from", wa_id)
    message.setdefault("timestamp", str(1767225600 + _counter[0]))
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": wa_id, "profile": {"name": name}}],
                            "messages": [message],
                        }
                    }
                ]
            }
        ],
    }


def send(wa_id, name, message):
    r = client.post("/webhooks/whatsapp", json=payload(wa_id, name, message))
    assert r.status_code == 200, r.text
    return [rec for rec in wa.DRY_RUN_LOG if rec["to"] == wa_id]


def text(body, forwarded=False):
    msg = {"type": "text", "text": {"body": body}}
    if forwarded:
        msg["context"] = {"forwarded": True}
    return msg


def card(name, phone, wa_id):
    return {
        "name": {"formatted_name": name},
        "phones": [{"phone": phone, "wa_id": wa_id}],
    }


def said(wa_id, needle):
    return any(needle in rec["text"] for rec in wa.DRY_RUN_LOG if rec["to"] == wa_id)


def pair_count(wa_id):
    owner = store.owner_id_for_address("whatsapp", wa_id)
    return len(store.pair_messages_for(owner, PETER_HASH))


def onboarded_with_peter():
    send(SARAH, "Sarah", text("hi"))
    send(SARAH, "Sarah", text("YES"))
    send(
        SARAH,
        "Sarah",
        {"type": "contacts", "contacts": [card("Peter", "+1 555-010-7777", PETER_WA)]},
    )
    send(SARAH, "Sarah", text("done"))
    wa.DRY_RUN_LOG.clear()


# ---------------------------------------------------------------------------
# Natural phrasing (no exact-match deafness)
# ---------------------------------------------------------------------------


def test_natural_phrasing_is_understood():
    onboarded_with_peter()
    send(SARAH, "Sarah", text("Thanks!"))
    assert said(SARAH, "Anytime")
    send(SARAH, "Sarah", text("thank you so much"))
    assert said(SARAH, "Anytime")
    send(SARAH, "Sarah", text("Who are you watching??"))
    assert said(SARAH, "Watching for you:")
    assert said(SARAH, "Peter")
    send(SARAH, "Sarah", text("is he cheating on me?"))
    assert said(SARAH, "never verdicts")
    assert pair_count(SARAH) == 0


def test_yes_with_punctuation_turns_it_on():
    send(SARAH, "Sarah", text("hi"))
    wa.DRY_RUN_LOG.clear()
    send(SARAH, "Sarah", text("Yes!"))
    assert said(SARAH, "You're in")


# ---------------------------------------------------------------------------
# Activity log + undo
# ---------------------------------------------------------------------------


def test_activity_and_undo():
    onboarded_with_peter()
    send(SARAH, "Sarah", text("i loved last night", forwarded=True))
    send(SARAH, "Sarah", text("come over", forwarded=True))
    assert pair_count(SARAH) == 2

    send(SARAH, "Sarah", text("activity"))
    assert said(SARAH, "Your latest entries")
    assert said(SARAH, "come over")

    wa.DRY_RUN_LOG.clear()
    send(SARAH, "Sarah", text("undo"))
    assert said(SARAH, "Removed the last logged message for Peter")
    assert pair_count(SARAH) == 1
    owner = store.owner_id_for_address("whatsapp", SARAH)
    assert len(store.signals_for_stream(owner, PETER_HASH)) == 1
    # Removing the chain tip leaves the evidence chain intact.
    assert evidence.verify_chain(owner, PETER_HASH)["intact"]

    wa.DRY_RUN_LOG.clear()
    send(SARAH, "Sarah", text("undo Peter"))
    assert said(SARAH, "Removed the last logged message for Peter")
    assert pair_count(SARAH) == 0

    wa.DRY_RUN_LOG.clear()
    send(SARAH, "Sarah", text("undo"))
    assert said(SARAH, "Nothing to undo")


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------


def test_pause_blocks_logging_until_resume():
    onboarded_with_peter()
    send(SARAH, "Sarah", text("pause"))
    assert said(SARAH, "Paused ✓")
    wa.DRY_RUN_LOG.clear()
    send(SARAH, "Sarah", text("i loved last night", forwarded=True))
    assert said(SARAH, "I'm paused, so I didn't log that")
    assert pair_count(SARAH) == 0
    send(SARAH, "Sarah", text("resume"))
    assert said(SARAH, "Watching again")
    wa.DRY_RUN_LOG.clear()
    send(SARAH, "Sarah", text("i loved last night", forwarded=True))
    assert said(SARAH, "✓ Logged 1 message for Peter")
    assert pair_count(SARAH) == 1


# ---------------------------------------------------------------------------
# Typed paste decisions (SMS has no buttons; typing must work too)
# ---------------------------------------------------------------------------


def test_typed_log_it_files_paste():
    onboarded_with_peter()
    send(SARAH, "Sarah", text(PASTE))
    assert said(SARAH, "haven't logged it")
    wa.DRY_RUN_LOG.clear()
    send(SARAH, "Sarah", text("log it"))
    assert said(SARAH, "✓ Logged 1 message for Peter")
    assert pair_count(SARAH) == 1


# ---------------------------------------------------------------------------
# Transport correctness
# ---------------------------------------------------------------------------


def test_multi_card_message_registers_every_card():
    send(SARAH, "Sarah", text("hi"))
    send(SARAH, "Sarah", text("YES"))
    send(
        SARAH,
        "Sarah",
        {
            "type": "contacts",
            "contacts": [
                card("Peter", "+1 555-010-7777", PETER_WA),
                card("Mia", "+1 555-010-8888", "15550108888"),
            ],
        },
    )
    owner = store.owner_id_for_address("whatsapp", SARAH)
    labels = [p["label"] for p in store.persons_for_owner(owner)]
    assert labels == ["Peter", "Mia"]


def test_unsupported_image_gets_guidance_not_silence():
    onboarded_with_peter()
    send(SARAH, "Sarah", {"type": "image", "image": {"id": "img-1"}})
    assert said(SARAH, "can't read screenshots")
    assert pair_count(SARAH) == 0


def test_outbox_notice_survives_send_failure(monkeypatch):
    onboarded_with_peter()
    owner = store.owner_id_for_address("whatsapp", SARAH)
    row_id = store.enqueue_outbox(owner, "whatsapp", SARAH, "Important notice XYZ", [])

    def boom(*args, **kwargs):
        raise RuntimeError("graph down")

    monkeypatch.setattr(wa, "send_message", boom)
    r = client.post("/webhooks/whatsapp", json=payload(LUNA, "Luna", text("hi")))
    assert r.status_code == 200
    pending_ids = [row["id"] for row in store.peek_outbox(channel="whatsapp")]
    assert row_id in pending_ids

    monkeypatch.undo()
    r = client.post("/webhooks/whatsapp", json=payload(LUNA, "Luna", text("hi")))
    assert r.status_code == 200
    pending_ids = [row["id"] for row in store.peek_outbox(channel="whatsapp")]
    assert row_id not in pending_ids
    assert said(SARAH, "Important notice XYZ")


def test_failed_processing_is_retried_not_swallowed(monkeypatch):
    send(SARAH, "Sarah", text("hi"))
    calls = {"n": 0}
    original = store.set_consent

    def flaky(owner_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db hiccup")
        return original(owner_id)

    monkeypatch.setattr(store, "set_consent", flaky)
    yes_payload = payload(SARAH, "Sarah", text("YES"))
    r = client_no_raise.post("/webhooks/whatsapp", json=yes_payload)
    assert r.status_code == 500
    # Meta retries the same delivery (same message id): it must be
    # processed now, not swallowed as a duplicate.
    r = client_no_raise.post("/webhooks/whatsapp", json=yes_payload)
    assert r.status_code == 200
    monkeypatch.undo()
    send(
        SARAH,
        "Sarah",
        {"type": "contacts", "contacts": [card("Peter", "+1 555-010-7777", PETER_WA)]},
    )
    assert said(SARAH, "Saved Peter")
