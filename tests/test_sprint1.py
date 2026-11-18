"""Sprint 1 hardening tests (2026-11-18 audits).

Covers the audit-driven fixes that the older suites predate:

- 'forget me' deletes ingest tokens (compliance 3.5)
- identifier + owner digests are peppered HMACs when
  OVERLAP_HASH_PEPPER is set, and matching still works (3.3 / S-3)
- agent state never stages full message bodies — snippets only (3.2)
- STOP is a real opt-out, not a pause: silence + no logging until
  START (1.2)
- WhatsApp notices are HELD outside the 24-hour window and delivered
  when the user next messages (1.1 / architecture P1)
- /evidence/verify never presents a pack's self-reported anchor as
  verified (6.1)
"""

import hashlib
import json
import os

import pytest
from fastapi.testclient import TestClient

from app import store, whatsapp as wa
from app.evidence import verify_pack
from app.main import app
from app.normalize import normalize_identifier

client = TestClient(app)

SARAH = "15551110001"
LUNA = "15551110002"
PETER_WA = "15550107777"
MIA_WA = "15550106666"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    os.environ["OVERLAP_DB_PATH"] = str(tmp_path / "sprint1.db")
    wa.DRY_RUN_LOG.clear()
    yield


_counter = [5000]


def payload(wa_id, name, message):
    _counter[0] += 1
    message.setdefault("id", f"wamid.s1-{_counter[0]}")
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


def card(name, phone):
    return {
        "type": "contacts",
        "contacts": [
            {"name": {"formatted_name": name}, "phones": [{"phone": phone}]}
        ],
    }


def onboard(wa_id, name):
    send(wa_id, name, text("hi"))
    send(wa_id, name, text("YES"))


def said(wa_id, needle):
    return any(needle in rec["text"] for rec in wa.DRY_RUN_LOG if rec["to"] == wa_id)


def signal_count(owner_id):
    with store._connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM signals WHERE owner_id = ?", (owner_id,)
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# Forget me kills tokens
# ---------------------------------------------------------------------------


def test_forget_me_deletes_ingest_tokens():
    token = store.create_ingest_token("owner-x")
    assert store.owner_for_token(token) == "owner-x"
    store.delete_owner_data("owner-x")
    assert store.owner_for_token(token) is None


# ---------------------------------------------------------------------------
# Peppered digests
# ---------------------------------------------------------------------------


def test_pepper_changes_digest_but_matching_survives(monkeypatch):
    monkeypatch.setenv("OVERLAP_HASH_PEPPER", "unit-test-pepper")
    a = normalize_identifier("+1 (555) 010-7777")["hash"]
    b = normalize_identifier("5550107777")["hash"]
    assert a == b, "same person must still match under the pepper"
    unpeppered = hashlib.sha256(b"+15550107777").hexdigest()
    assert a != unpeppered, "peppered digest must not be a bare SHA-256"
    owner = store.owner_id_for_address("whatsapp", SARAH)
    assert owner != "u_" + hashlib.sha256(
        f"whatsapp:{SARAH}".encode()
    ).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Agent state holds snippets only
# ---------------------------------------------------------------------------


def _raw_context(owner_id):
    with store._connect() as conn:
        row = conn.execute(
            "SELECT context FROM agent_state WHERE owner_id = ?", (owner_id,)
        ).fetchone()
    return row["context"] if row else "{}"


def test_staged_burst_is_snippet_capped():
    onboard(SARAH, "Sarah")
    send(SARAH, "Sarah", card("Peter", PETER_WA))
    send(SARAH, "Sarah", card("Mia", MIA_WA))  # 2 people -> bursts stage
    long_body = "x" * 300
    send(SARAH, "Sarah", text(long_body, forwarded=True))
    owner = store.owner_id_for_address("whatsapp", SARAH)
    raw = _raw_context(owner)
    assert long_body not in raw
    ctx = json.loads(raw)
    assert all(len(e["text"]) <= 80 for e in ctx.get("pending", []))


def test_staged_paste_is_snippet_capped():
    onboard(SARAH, "Sarah")
    long_line = "Peter: " + ("y" * 200)
    paste = "\n".join(
        [
            "[9/6/26, 2:14:03 PM] Peter: hey",
            f"[9/6/26, 2:15:03 PM] {long_line}",
            "[9/6/26, 2:16:03 PM] Sarah: hi",
            "[9/6/26, 2:17:03 PM] Peter: rooftop tonight?",
        ]
    )
    send(SARAH, "Sarah", text(paste))
    owner = store.owner_id_for_address("whatsapp", SARAH)
    ctx = json.loads(_raw_context(owner))
    staged = ctx["pending_paste"]["text"]
    assert all(len(line) <= 80 for line in staged.splitlines())
    assert "y" * 200 not in staged


# ---------------------------------------------------------------------------
# STOP is a real opt-out
# ---------------------------------------------------------------------------


def test_stop_silences_everything_until_start():
    onboard(SARAH, "Sarah")
    send(SARAH, "Sarah", card("Peter", PETER_WA))
    send(SARAH, "Sarah", text("done"))
    owner = store.owner_id_for_address("whatsapp", SARAH)

    send(SARAH, "Sarah", text("see you tonight", forwarded=True))
    assert signal_count(owner) == 1

    send(SARAH, "Sarah", text("stop"))
    assert said(SARAH, "opted out")
    log_len = len(wa.DRY_RUN_LOG)

    # While opted out: forwards are not logged, commands get silence.
    send(SARAH, "Sarah", text("another one", forwarded=True))
    send(SARAH, "Sarah", text("status"))
    assert signal_count(owner) == 1
    assert len(wa.DRY_RUN_LOG) == log_len

    send(SARAH, "Sarah", text("start"))
    assert said(SARAH, "back in")
    send(SARAH, "Sarah", text("and another", forwarded=True))
    assert signal_count(owner) == 2


# ---------------------------------------------------------------------------
# WhatsApp 24-hour window
# ---------------------------------------------------------------------------


def test_notice_held_outside_window_delivered_on_next_message():
    onboard(SARAH, "Sarah")
    luna_owner = store.owner_id_for_address("whatsapp", LUNA)
    store.get_or_create_chat_user("whatsapp", LUNA, "Luna")
    store.enqueue_outbox(luna_owner, "whatsapp", LUNA, "🔔 held notice", [])

    # Sarah's message triggers a drain — Luna's notice must be HELD
    # (Luna has never messaged: no open window).
    send(SARAH, "Sarah", text("status"))
    assert not said(LUNA, "held notice")
    assert store.peek_outbox(channel="whatsapp"), "held row must stay queued"

    # Luna messages -> her window opens -> the notice drains to her.
    send(LUNA, "Luna", text("hi"))
    assert said(LUNA, "held notice")
    assert store.peek_outbox(channel="whatsapp") == []


def test_in_window_helper():
    owner = store.owner_id_for_address("whatsapp", LUNA)
    store.get_or_create_chat_user("whatsapp", LUNA, "Luna")
    assert wa._in_window(owner) is False
    store.touch_inbound(owner)
    assert wa._in_window(owner) is True
    with store._connect() as conn:
        conn.execute(
            "UPDATE chat_users SET last_inbound_at = '2020-01-01 00:00:00' "
            "WHERE owner_id = ?",
            (owner,),
        )
    assert wa._in_window(owner) is False


# ---------------------------------------------------------------------------
# Verify honesty
# ---------------------------------------------------------------------------


def test_verify_pack_never_claims_anchor_verified():
    pack = {
        "owner_id": "u_x",
        "person_hash": "abc",
        "entries": [],
        "anchors": [{"status": "submitted"}],
    }
    result = verify_pack(pack)
    assert result["anchor_status"] == "anchored"  # the pack's claim…
    assert result["anchor_verified"] is False  # …is never vouched for
