import os

import pytest
from fastapi.testclient import TestClient

from app import bluebubbles as bb
from app import store
from app.main import app
from app.normalize import normalize_identifier

client = TestClient(app)

USER = "+15551110001"
MIA_PHONE = "+15552224821"
MIA_HASH = normalize_identifier(MIA_PHONE)["hash"]

VCARD = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Mia\r\n"
    "TEL;type=CELL:+1 555-222-4821\r\nEND:VCARD\r\n"
)

_counter = [0]


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    os.environ["OVERLAP_DB_PATH"] = str(tmp_path / "bb-test.db")
    for var in (
        "BLUEBUBBLES_SERVER_URL",
        "BLUEBUBBLES_PASSWORD",
        "BLUEBUBBLES_WEBHOOK_SECRET",
    ):
        os.environ.pop(var, None)
    bb.DRY_RUN_LOG.clear()
    bb.CHAT_GUIDS.clear()
    yield


def post(text="", attachments=None, **data_extra):
    _counter[0] += 1
    data = {
        "guid": f"msg-{_counter[0]}",
        "text": text,
        "isFromMe": False,
        "dateCreated": 1763500000000 + _counter[0] * 1000,
        "handle": {"address": USER},
        "chats": [{"guid": f"iMessage;-;{USER}"}],
        "attachments": attachments or [],
    }
    data.update(data_extra)
    return client.post("/webhooks/bluebubbles", json={"type": "new-message", "data": data})


def last_text():
    return bb.DRY_RUN_LOG[-1]["text"]


def onboard_with_mia(monkeypatch):
    post("hi")
    post("YES")
    monkeypatch.setattr(bb, "fetch_attachment_bytes", lambda guid: VCARD.encode())
    post(
        attachments=[
            {"guid": "att-1", "mimeType": "text/vcard", "transferName": "Mia.vcf"}
        ]
    )


def test_welcome_then_consent_asks_for_contacts():
    assert post("hi").status_code == 200
    assert "I'm OVERLAP" in last_text()
    post("YES")
    assert "contact" in last_text().lower()


def test_vcard_attachment_registers_person(monkeypatch):
    onboard_with_mia(monkeypatch)
    post("status")
    assert "Mia" in last_text()
    assert bb.CHAT_GUIDS[USER] == f"iMessage;-;{USER}"


def test_paste_is_staged_then_logged_on_yes(monkeypatch):
    onboard_with_mia(monkeypatch)
    paste = (
        "Mia: hey you\n"
        "Mia: i loved last night\n"
        "Mia: don't tell anyone\n"
        "Mia: text me when you're free"
    )
    post(paste)
    assert "LOG IT" in last_text()
    owner_id = store.owner_id_for_address("bluebubbles", USER)
    assert store.signals_for_stream(owner_id, MIA_HASH) == []
    post("log it")
    assert "✓ Logged" in last_text()
    assert len(store.signals_for_stream(owner_id, MIA_HASH)) == 1


def test_webhook_key_enforced_when_set():
    os.environ["BLUEBUBBLES_WEBHOOK_SECRET"] = "s3cret"
    payload = {"type": "new-message", "data": {"guid": "m1", "text": "hi",
               "handle": {"address": USER}, "chats": [], "attachments": []}}
    assert client.post("/webhooks/bluebubbles", json=payload).status_code == 403
    ok = client.post("/webhooks/bluebubbles?key=s3cret", json=payload)
    assert ok.status_code == 200


def test_own_messages_groups_and_other_events_are_ignored():
    post("hi")
    n = len(bb.DRY_RUN_LOG)
    post("my own send", isFromMe=True)
    post("group hi", chats=[{"guid": "iMessage;+;chat123"}])
    client.post(
        "/webhooks/bluebubbles",
        json={"type": "typing-indicator", "data": {"guid": "t1"}},
    )
    assert len(bb.DRY_RUN_LOG) == n


def test_media_only_gets_guidance():
    post(attachments=[{"guid": "att-9", "mimeType": "image/jpeg", "transferName": "p.jpg"}])
    assert "work with text" in last_text()
