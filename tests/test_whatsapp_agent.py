import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import store, whatsapp as wa
from app.export_parser import dedupe_key, parse_export
from app.main import app
from app.normalize import normalize_identifier

client = TestClient(app)
FIXTURES = Path(__file__).resolve().parent.parent / "scripts" / "fixtures"

SARAH = "15551110001"
LUNA = "15551110002"
PETER_WA = "15550107777"
PETER_HASH = normalize_identifier(PETER_WA)["hash"]


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    os.environ["OVERLAP_DB_PATH"] = str(tmp_path / "wa-test.db")
    wa.DRY_RUN_LOG.clear()
    yield


@pytest.fixture
def media(monkeypatch):
    files = {
        "media-sarah": (FIXTURES / "sarah_peter.txt").read_bytes(),
        "media-luna": (FIXTURES / "luna_peter.txt").read_bytes(),
    }
    monkeypatch.setattr(wa, "fetch_media_bytes", lambda mid: files[mid])
    return files


_counter = [1000]


def payload(wa_id, name, message):
    _counter[0] += 1
    message.setdefault("id", f"wamid.test{_counter[0]}")
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


def doc(media_id):
    return {
        "type": "document",
        "document": {"id": media_id, "filename": "chat.txt", "mime_type": "text/plain"},
    }


def onboard(wa_id, name):
    send(wa_id, name, text("hi"))
    send(wa_id, name, text("YES"))


def said(wa_id, needle):
    return any(needle in rec["text"] for rec in wa.DRY_RUN_LOG if rec["to"] == wa_id)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parser_ios_format():
    parsed = parse_export((FIXTURES / "sarah_peter.txt").read_text())
    assert parsed["names"] == ["Peter", "Sarah"]
    assert parsed["system_lines"] == 1
    assert len(parsed["messages"]) == 13
    first = parsed["messages"][0]
    assert first["sender"] == "Peter"
    assert first["sent_at"] == "2026-09-06T14:14:03"
    # Multi-line message is joined.
    assert "rooftop" in parsed["messages"][-1]["text"]


def test_parser_android_format_and_day_detection():
    parsed = parse_export((FIXTURES / "luna_peter.txt").read_text())
    assert parsed["names"] == ["Luna", "Peter"]
    spike = [m for m in parsed["messages"] if m["text"].startswith("can't sleep")]
    assert len(spike) >= 5
    assert spike[0]["sent_at"] == "2026-10-06T01:10:00"


def test_parser_dayfirst_locale():
    parsed = parse_export(
        "24/08/2026, 21:41 - Ana: hola\n24/08/2026, 21:42 - Ben: hey\n"
    )
    assert parsed["messages"][0]["sent_at"] == "2026-08-24T21:41:00"


def test_dedupe_key_stable():
    a = dedupe_key("2026-10-06T01:10:00", "Peter", "can't sleep")
    b = dedupe_key("2026-10-06T01:10:00", "Peter", "can't sleep")
    c = dedupe_key("2026-10-06T01:10:00", "Peter", "different")
    assert a == b != c


# ---------------------------------------------------------------------------
# Webhook + consent
# ---------------------------------------------------------------------------


def test_webhook_verification():
    r = client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "overlap-dev-verify-token",
            "hub.challenge": "12345",
        },
    )
    assert r.status_code == 200 and r.text == "12345"
    bad = client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "12345",
        },
    )
    assert bad.status_code == 403


def test_consent_gate_blocks_storage():
    send(SARAH, "Sarah", card("+1 555-010-7777", PETER_WA))
    owner = store.owner_id_for_address("whatsapp", SARAH)
    assert store.persons_for_owner(owner) == []
    assert said(SARAH, "Reply YES")


def test_once_per_person_ever():
    onboard(SARAH, "Sarah")
    send(SARAH, "Sarah", card("+1 555-010-7777", PETER_WA))
    assert said(SARAH, "Saved Peter")
    wa.DRY_RUN_LOG.clear()
    send(SARAH, "Sarah", card("5550107777"))
    assert said(SARAH, "already saved")
    owner = store.owner_id_for_address("whatsapp", SARAH)
    assert len(store.persons_for_owner(owner)) == 1


def test_onboarding_asks_up_to_three_cards_then_done():
    onboard(SARAH, "Sarah")
    assert said(SARAH, "up to 3")
    send(SARAH, "Sarah", card("+1 555-010-7777", PETER_WA))
    assert said(SARAH, "Saved Peter")
    assert said(SARAH, "1 of 3")
    mia = {
        "type": "contacts",
        "contacts": [
            {
                "name": {"formatted_name": "Mia"},
                "phones": [{"phone": "+1 555-010-8888", "wa_id": "15550108888"}],
            }
        ],
    }
    send(SARAH, "Sarah", mia)
    assert said(SARAH, "Saved Mia")
    assert said(SARAH, "2 of 3")
    wa.DRY_RUN_LOG.clear()
    send(SARAH, "Sarah", text("done"))
    assert said(SARAH, "You're set — watching Peter, Mia")
    owner = store.owner_id_for_address("whatsapp", SARAH)
    assert store.get_agent_state(owner)["state"] == "idle"


def test_onboarding_third_card_finishes_and_skip_works():
    onboard(SARAH, "Sarah")
    send(SARAH, "Sarah", card("+1 555-010-7777", PETER_WA))
    send(SARAH, "Sarah", {"type": "contacts", "contacts": [{"name": {"formatted_name": "Mia"}, "phones": [{"phone": "+1 555-010-8888", "wa_id": "15550108888"}]}]})
    send(
        SARAH,
        "Sarah",
        {"type": "contacts", "contacts": [{"name": {"formatted_name": "Ana"}, "phones": [{"phone": "+1 555-010-9999", "wa_id": "15550109999"}]}]},
    )
    assert said(SARAH, "That's 3 — you're set")

    onboard(LUNA, "Luna")
    wa.DRY_RUN_LOG.clear()
    send(LUNA, "Luna", text("skip"))
    assert said(LUNA, "No problem — share a contact card any time")
    owner = store.owner_id_for_address("whatsapp", LUNA)
    assert store.get_agent_state(owner)["state"] == "idle"
    assert store.persons_for_owner(owner) == []


# ---------------------------------------------------------------------------
# Full story through the bot
# ---------------------------------------------------------------------------


def test_full_story_match_flag_review(media):
    onboard(SARAH, "Sarah")
    send(SARAH, "Sarah", card("+1 555-010-7777", PETER_WA))
    send(SARAH, "Sarah", doc("media-sarah"))
    assert said(SARAH, "together since")
    sarah_owner = store.owner_id_for_address("whatsapp", SARAH)
    pair = store.get_pair_state(sarah_owner, PETER_HASH)
    assert pair["relationship_state"] == "together"
    assert pair["since_iso"].startswith("2026-10-03")

    onboard(LUNA, "Luna")
    send(LUNA, "Luna", card("5550107777"))
    send(LUNA, "Luna", doc("media-luna"))
    assert said(LUNA, "Potential overlap")
    assert said(SARAH, "Pattern change for Peter")
    flag_text = next(
        rec["text"] for rec in wa.DRY_RUN_LOG if "Pattern change" in rec["text"]
    )
    assert "Frequency jumped" in flag_text
    assert "Late-night share rose" in flag_text
    assert "Affectionate language is new" not in flag_text

    # No review yet -> API alert is blocked.
    match_id = f"match_{PETER_HASH[:16]}"
    blocked = client.post(
        f"/matches/{match_id}/alert", json={"owner_id": sarah_owner}
    )
    assert blocked.status_code == 409

    # Sarah taps "Keep alerts on" in the chat.
    send(
        SARAH,
        "Sarah",
        {
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {
                    "id": f"review:{PETER_HASH[:16]}:confirmed",
                    "title": "Keep alerts on",
                },
            },
        },
    )
    assert said(SARAH, "Alerts on for this match")
    got = client.get(f"/matches/{match_id}", params={"owner_id": sarah_owner})
    assert got.json()["your_review"] == "confirmed"


def test_export_resend_dedupes(media):
    onboard(SARAH, "Sarah")
    send(SARAH, "Sarah", card("+1 555-010-7777", PETER_WA))
    send(SARAH, "Sarah", doc("media-sarah"))
    wa.DRY_RUN_LOG.clear()
    send(SARAH, "Sarah", doc("media-sarah"))
    assert said(SARAH, "0 new messages")


def test_forward_attribution_single_person(media):
    onboard(SARAH, "Sarah")
    send(SARAH, "Sarah", card("+1 555-010-7777", PETER_WA))
    wa.DRY_RUN_LOG.clear()
    send(SARAH, "Sarah", text("i loved last night", forwarded=True))
    assert said(SARAH, "✓ Logged 1 message for Peter")


# ---------------------------------------------------------------------------
# SMS agent + export API
# ---------------------------------------------------------------------------


def test_sms_agent_flow():
    r = client.post(
        "/webhooks/sms", data={"From": "+15552220001", "Body": "hi", "MessageSid": "SM1"}
    )
    assert "Reply YES" in r.text
    r = client.post(
        "/webhooks/sms",
        data={"From": "+15552220001", "Body": "YES", "MessageSid": "SM2"},
    )
    assert "You're in" in r.text
    r = client.post(
        "/webhooks/sms",
        data={"From": "+15552220001", "Body": "status", "MessageSid": "SM3"},
    )
    assert "not watching anyone" in r.text


def test_export_endpoint_with_token():
    token = client.post("/tokens", json={"owner_id": "api-sarah"}).json()["ingest_token"]
    body = {
        "partner_identifier": "+1 555-010-7777",
        "owner_name": "Sarah",
        "export_text": (FIXTURES / "sarah_peter.txt").read_text(),
    }
    r = client.post("/ingest/export", json=body, headers={"X-Overlap-Token": token})
    assert r.status_code == 201, r.text
    assert r.json()["relationship"]["relationship_state"] == "together"
    again = client.post("/ingest/export", json=body, headers={"X-Overlap-Token": token})
    assert any("0 new messages" in t for t in again.json()["agent_replies"])
    bad = client.post("/ingest/export", json=body, headers={"X-Overlap-Token": "nope"})
    assert bad.status_code == 401



# ---------------------------------------------------------------------------
# Live site endpoints
# ---------------------------------------------------------------------------


def test_home_page_and_api_root():
    r = client.get("/")
    assert r.status_code == 200
    assert "Know if" in r.text
    assert "only one." in r.text
    assert client.get("/api").json()["status"] == "ok"


def test_export_file_endpoint_whoami_and_flags():
    tok_a = client.post("/tokens", json={"owner_id": "site-sarah"}).json()["ingest_token"]
    files = {
        "file": ("chat.txt", (FIXTURES / "sarah_peter.txt").read_bytes(), "text/plain")
    }
    r = client.post(
        "/ingest/export-file",
        files=files,
        data={"partner_identifier": "+1 555-010-7777", "owner_name": "Sarah"},
        headers={"X-Overlap-Token": tok_a},
    )
    assert r.status_code == 201, r.text
    assert r.json()["relationship"]["relationship_state"] == "together"
    assert r.json()["flags"] == []

    w = client.get("/ingest/whoami", headers={"X-Overlap-Token": tok_a})
    assert w.json()["owner_id"] == "site-sarah"
    assert client.get("/ingest/whoami", headers={"X-Overlap-Token": "bad"}).status_code == 401

    tok_b = client.post("/tokens", json={"owner_id": "site-luna"}).json()["ingest_token"]
    files_b = {
        "file": ("chat.txt", (FIXTURES / "luna_peter.txt").read_bytes(), "text/plain")
    }
    rb = client.post(
        "/ingest/export-file",
        files=files_b,
        data={"partner_identifier": "5550107777", "owner_name": "Luna"},
        headers={"X-Overlap-Token": tok_b},
    )
    assert rb.status_code == 201, rb.text
    assert rb.json()["potential_match"] is not None

    fa = client.get("/ingest/flags", headers={"X-Overlap-Token": tok_a})
    assert fa.status_code == 200
    flags = fa.json()["flags"]
    assert len(flags) == 1 and flags[0]["partner_label"] == "Peter"
    assert any(rep["flagged"] for rep in flags[0]["reports"])
