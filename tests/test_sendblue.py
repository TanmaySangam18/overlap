import os

import pytest
from fastapi.testclient import TestClient

from app import sendblue as sb
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
    os.environ["OVERLAP_DB_PATH"] = str(tmp_path / "sb-test.db")
    for var in (
        "SENDBLUE_API_KEY_ID",
        "SENDBLUE_API_SECRET_KEY",
        "SENDBLUE_FROM_NUMBER",
        "SENDBLUE_WEBHOOK_SECRET",
    ):
        os.environ.pop(var, None)
    sb.DRY_RUN_LOG.clear()
    yield


def post(content="", media_url="", **extra):
    _counter[0] += 1
    payload = {
        "from_number": USER,
        "to_number": "+15559990000",
        "content": content,
        "media_url": media_url,
        "service": "iMessage",
        "group_id": None,
        "date_sent": f"2026-11-18T15:{_counter[0] % 60:02d}:00Z",
    }
    payload.update(extra)
    return client.post("/webhooks/sendblue", json=payload)


def last_text():
    return sb.DRY_RUN_LOG[-1]["text"]


def onboard_with_mia(monkeypatch):
    post("hi")
    post("YES")
    monkeypatch.setattr(sb, "_fetch_text", lambda url: VCARD)
    post(media_url="https://storage.sendblue.co/card.vcf")


def test_welcome_then_consent_asks_for_contacts():
    assert post("hi").status_code == 200
    assert "I'm OVERLAP" in last_text()
    post("YES")
    assert "contact" in last_text().lower()


def test_vcard_contact_registers_person(monkeypatch):
    onboard_with_mia(monkeypatch)
    post("status")
    assert "Mia" in last_text()


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
    owner_id = store.owner_id_for_address("sendblue", USER)
    assert store.signals_for_stream(owner_id, MIA_HASH) == []
    post("log it")
    assert "✓ Logged" in last_text()
    # The staged paste files as one signal carrying the whole block.
    assert len(store.signals_for_stream(owner_id, MIA_HASH)) == 1


def test_webhook_secret_enforced_when_set():
    os.environ["SENDBLUE_WEBHOOK_SECRET"] = "s3cret"
    payload = {"from_number": USER, "content": "hi", "date_sent": "2026-11-18T15:00:00Z"}
    assert client.post("/webhooks/sendblue", json=payload).status_code == 403
    ok = client.post(
        "/webhooks/sendblue", json=payload, headers={"sb-signing-secret": "s3cret"}
    )
    assert ok.status_code == 200


def test_outbound_echo_and_groups_are_ignored():
    post("hi")
    n = len(sb.DRY_RUN_LOG)
    post("echo", is_outbound=True)
    post("group hi", group_id="group-1")
    assert len(sb.DRY_RUN_LOG) == n


def test_media_only_gets_guidance():
    post(media_url="https://storage.sendblue.co/photo.jpg")
    assert "work with text" in last_text()
