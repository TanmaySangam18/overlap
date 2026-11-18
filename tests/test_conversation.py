"""Typed text is conversation with the agent — never a log entry.

Only forwards (and exports/screenshots) are evidence. A paste that
looks like chat lines is staged and filed only after the user taps
"Log it".
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import store, whatsapp as wa
from app.main import app
from app.normalize import normalize_identifier

client = TestClient(app)

SARAH = "15551110001"
PETER_WA = "15550107777"
PETER_HASH = normalize_identifier(PETER_WA)["hash"]

PASTE = (
    "[18/11/2026, 9:41 PM] Peter: are you up\n"
    "[18/11/2026, 9:42 PM] Peter: come over"
)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    os.environ["OVERLAP_DB_PATH"] = str(tmp_path / "conv-test.db")
    wa.DRY_RUN_LOG.clear()
    yield


_counter = [5000]


def payload(wa_id, name, message):
    _counter[0] += 1
    message.setdefault("id", f"wamid.conv{_counter[0]}")
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


def card(phone, wa_id=None):
    phones = [{"phone": phone}]
    if wa_id:
        phones[0]["wa_id"] = wa_id
    return {
        "type": "contacts",
        "contacts": [{"name": {"formatted_name": "Peter"}, "phones": phones}],
    }


def button(button_id, title):
    return {
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": button_id, "title": title},
        },
    }


def said(wa_id, needle):
    return any(needle in rec["text"] for rec in wa.DRY_RUN_LOG if rec["to"] == wa_id)


def pair_count(wa_id):
    owner = store.owner_id_for_address("whatsapp", wa_id)
    return len(store.pair_messages_for(owner, PETER_HASH))


def onboarded_with_peter():
    send(SARAH, "Sarah", text("hi"))
    send(SARAH, "Sarah", text("YES"))
    send(SARAH, "Sarah", card("+1 555-010-7777", PETER_WA))
    send(SARAH, "Sarah", text("done"))
    wa.DRY_RUN_LOG.clear()


def test_typed_text_is_conversation_not_logged():
    onboarded_with_peter()
    send(SARAH, "Sarah", text("thanks"))
    assert said(SARAH, "Anytime")
    send(SARAH, "Sarah", text("do you think he is cheating on me?"))
    assert said(SARAH, "never verdicts")
    send(SARAH, "Sarah", text("just venting for a second"))
    assert said(SARAH, "not logging that")
    assert not said(SARAH, "✓ Logged")
    assert pair_count(SARAH) == 0


def test_forward_still_logs():
    onboarded_with_peter()
    send(SARAH, "Sarah", text("i loved last night", forwarded=True))
    assert said(SARAH, "✓ Logged 1 message for Peter")
    assert pair_count(SARAH) == 1


def test_chat_paste_asks_and_talk_files_nothing():
    onboarded_with_peter()
    send(SARAH, "Sarah", text(PASTE))
    assert said(SARAH, "haven't logged it")
    assert pair_count(SARAH) == 0
    wa.DRY_RUN_LOG.clear()
    send(SARAH, "Sarah", button("paste:talk", "Just talking"))
    assert said(SARAH, "Nothing logged")
    assert pair_count(SARAH) == 0


def test_chat_paste_log_it_files_it():
    onboarded_with_peter()
    send(SARAH, "Sarah", text(PASTE))
    wa.DRY_RUN_LOG.clear()
    send(SARAH, "Sarah", button("paste:log", "Log it"))
    assert said(SARAH, "✓ Logged 1 message for Peter")
    assert pair_count(SARAH) == 1
