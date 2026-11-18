"""Evidence layer: hash-chained signals on every ingest path, tamper
detection, legacy (pre-chain) rows, OpenTimestamps anchoring with a
mocked calendar (tests never touch the network), evidence-pack privacy
(a pack holds only its owner's data), public pack verification, and
the agent's 'evidence' command.
"""

import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import evidence, ratelimit, store, whatsapp as wa
from app.main import app
from app.normalize import normalize_identifier

client = TestClient(app)

PETER_WA = "15550107777"
PETER = normalize_identifier(PETER_WA)
PETER_HASH = PETER["hash"]
MIA_WA = "15550108888"
MIA = normalize_identifier(MIA_WA)
MIA_HASH = MIA["hash"]
SARAH = "15551110001"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    os.environ["OVERLAP_DB_PATH"] = str(tmp_path / "evidence-test.db")
    ratelimit.reset()
    wa.DRY_RUN_LOG.clear()
    yield


def make_token(owner_id):
    return client.post("/tokens", json={"owner_id": owner_id}).json()["ingest_token"]


def post_signal(owner_id, text, sender=MIA_WA, sent_at="2026-11-10T23:15:00"):
    r = client.post(
        "/signals",
        json={
            "owner_id": owner_id,
            "source": "whatsapp",
            "conversation_id": "c1",
            "sender_identifier": sender,
            "message_text": text,
            "sent_at": sent_at,
            "consent_confirmed": True,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def assert_stream_chained(owner_id, person_hash, count):
    rows = store.signals_for_stream(owner_id, person_hash)
    assert len(rows) == count
    assert all(r["chain_hash"] for r in rows)
    assert rows[0]["prev_hash"] == "GENESIS"
    for prev, cur in zip(rows, rows[1:]):
        assert cur["prev_hash"] == prev["chain_hash"]
    result = evidence.verify_chain(owner_id, person_hash)
    assert result["intact"] is True
    assert result["chained_entries"] == count
    assert result["first_break"] is None
    return rows


# ---------------------------------------------------------------------------
# Chaining on every ingest path
# ---------------------------------------------------------------------------


def test_chain_appends_on_signals_api():
    post_signal("chain-api", "one")
    post_signal("chain-api", "two")
    post_signal("chain-api", "three")
    assert_stream_chained("chain-api", MIA_HASH, 3)


def test_chain_appends_on_shortcut_ingest():
    token = make_token("chain-shortcut")
    for text in ("first", "second"):
        r = client.post(
            "/ingest/shortcut",
            json={
                "sender_identifier": PETER_WA,
                "message_text": text,
                "sent_at": "2026-11-10T22:00:00",
            },
            headers={"X-Overlap-Token": token},
        )
        assert r.status_code == 201, r.text
    assert_stream_chained("chain-shortcut", PETER_HASH, 2)


def test_chain_appends_on_screenshot_ingest():
    owner = "chain-shot"
    store.add_person(owner, "Peter", PETER)
    token = make_token(owner)
    ocr = "9:41\nPeter\nonline\nhey, are we still on for tomorrow?\ni loved last night\n"
    r = client.post(
        "/ingest/screenshot",
        json={"text": ocr, "source_app": "whatsapp"},
        headers={"X-Overlap-Token": token},
    )
    assert r.status_code == 201, r.text
    assert r.json()["filed"] == 2
    assert_stream_chained(owner, PETER_HASH, 2)


def test_chain_appends_on_export_ingest():
    token = make_token("chain-export")
    export_text = (
        "10/11/2026, 23:41 - Mia: i loved last night\n"
        "10/11/2026, 23:43 - Sarah: wow ok\n"
        "10/11/2026, 23:44 - Mia: see you tomorrow\n"
    )
    r = client.post(
        "/ingest/export",
        json={
            "partner_identifier": MIA_WA,
            "owner_name": "Sarah",
            "export_text": export_text,
        },
        headers={"X-Overlap-Token": token},
    )
    assert r.status_code == 201, r.text
    assert_stream_chained("chain-export", MIA_HASH, 2)


# ---------------------------------------------------------------------------
# Tamper detection + legacy rows
# ---------------------------------------------------------------------------


def test_tamper_breaks_chain_at_the_edited_entry():
    store.add_signal("tamper-owner", "whatsapp", "c1", PETER, "first", "2026-11-01T10:00:00")
    second = store.add_signal(
        "tamper-owner", "whatsapp", "c1", PETER, "second", "2026-11-01T10:01:00"
    )
    store.add_signal("tamper-owner", "whatsapp", "c1", PETER, "third", "2026-11-01T10:02:00")
    assert evidence.verify_chain("tamper-owner", PETER_HASH)["intact"] is True

    conn = sqlite3.connect(os.environ["OVERLAP_DB_PATH"])
    conn.execute(
        "UPDATE signals SET message_snippet = 'forged after the fact' WHERE id = ?",
        (second,),
    )
    conn.commit()
    conn.close()

    result = evidence.verify_chain("tamper-owner", PETER_HASH)
    assert result["intact"] is False
    assert result["first_break"] == second
    assert result["entries"] == 3
    assert result["chained_entries"] == 3


def test_legacy_rows_are_a_tolerated_segment():
    store.hashes_for_owner("legacy-owner")  # forces schema + migration
    conn = sqlite3.connect(os.environ["OVERLAP_DB_PATH"])
    conn.execute(
        "INSERT INTO signals (owner_id, source, conversation_id, "
        "identifier_type, identifier_hash, identifier_masked, "
        "message_snippet, sent_at, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-owner",
            "whatsapp",
            "c1",
            PETER["type"],
            PETER_HASH,
            PETER["masked"],
            "written before chaining existed",
            "2026-10-01T09:00:00",
            "2026-10-01 09:00:05",
        ),
    )
    conn.commit()
    conn.close()

    store.add_signal("legacy-owner", "whatsapp", "c1", PETER, "chained one", "2026-11-01T10:00:00")
    store.add_signal("legacy-owner", "whatsapp", "c1", PETER, "chained two", "2026-11-01T10:01:00")

    result = evidence.verify_chain("legacy-owner", PETER_HASH)
    assert result["entries"] == 3
    assert result["chained_entries"] == 2
    assert result["intact"] is True
    assert result["first_break"] is None


# ---------------------------------------------------------------------------
# Anchoring (calendar mocked — no network in tests)
# ---------------------------------------------------------------------------


def _ok_submit(root_hash):
    return b"\xde\xad\xbe\xef"


def _fail_submit(root_hash):
    raise ConnectionError("calendar unreachable")


def test_anchor_root_and_successful_submission():
    store.add_signal("anchor-owner", "imessage", "c1", PETER, "hello", "2026-11-01T09:00:00")
    root = evidence.anchor_root()
    assert root is not None
    assert root == evidence.anchor_root()  # stable while tips don't move

    result = evidence.run_anchor(submit_fn=_ok_submit)
    assert result["root"] == root
    assert result["created"]["status"] == "submitted"
    records = store.anchor_records()
    assert len(records) == 1
    assert records[0]["status"] == "submitted"
    assert records[0]["proof"] == b"\xde\xad\xbe\xef".hex()

    # Unchanged root: a later run creates nothing and submits nothing.
    again = evidence.run_anchor(submit_fn=_ok_submit)
    assert again["created"] is None
    assert again["attempts"] == []
    assert len(store.anchor_records()) == 1


def test_anchor_failure_stays_pending_and_next_run_retries():
    store.add_signal("anchor-owner", "imessage", "c1", PETER, "hello", "2026-11-01T09:00:00")
    result = evidence.run_anchor(submit_fn=_fail_submit)
    assert result["created"]["status"] == "pending"
    assert "calendar unreachable" in result["created"]["error"]
    records = store.anchor_records()
    assert len(records) == 1
    assert records[0]["status"] == "pending"
    assert records[0]["proof"] is None

    retry = evidence.run_anchor(submit_fn=_ok_submit)
    assert retry["created"] is None  # same root — retried, not duplicated
    assert [a["status"] for a in retry["attempts"]] == ["submitted"]
    assert store.anchor_records()[0]["status"] == "submitted"
    assert len(store.anchor_records()) == 1


def test_anchor_root_is_none_without_chained_entries():
    assert evidence.anchor_root() is None
    result = evidence.run_anchor(submit_fn=_ok_submit)
    assert result["root"] is None
    assert store.anchor_records() == []


# ---------------------------------------------------------------------------
# Evidence packs: privacy, metadata, verification round-trip
# ---------------------------------------------------------------------------


def _pack_for(owner_id, person="Mia"):
    token = make_token(owner_id)
    r = client.get(
        "/evidence",
        params={"person": person},
        headers={"X-Overlap-Token": token},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_evidence_pack_contains_only_the_callers_data():
    for owner in ("pack-alice", "pack-bob"):
        store.add_person(owner, "Mia", MIA)
    post_signal("pack-alice", "alice saw her tuesday")
    post_signal("pack-bob", "bob-secret-rendezvous-42")

    pack = _pack_for("pack-alice")
    blob = json.dumps(pack)
    assert "bob-secret-rendezvous-42" not in blob  # other owner's content
    assert "pack-bob" not in blob  # other owner never identified
    assert "alice saw her tuesday" in blob
    assert pack["owner_id"] == "pack-alice"
    assert pack["person"]["masked"] == MIA["masked"]
    assert pack["chain_verification"]["intact"] is True
    assert pack["entries"][0]["chain_hash"]
    # Match metadata: dates and counts only.
    assert pack["match"]["exists"] is True
    assert pack["match"]["distinct_owner_count"] == 2
    assert pack["match"]["signal_count"] == 2
    assert pack["source_strength"][0]["label"] == "forward"
    assert "not proof of a relationship or misconduct" in pack["disclaimer"]


def test_evidence_pack_auth_and_unknown_person():
    token = make_token("pack-carol")
    r = client.get("/evidence", params={"person": "Mia"})
    assert r.status_code == 401
    r = client.get(
        "/evidence",
        params={"person": "Mia"},
        headers={"X-Overlap-Token": "ovl_not-a-real-token"},
    )
    assert r.status_code == 401
    r = client.get(
        "/evidence",
        params={"person": "Nobody"},
        headers={"X-Overlap-Token": token},
    )
    assert r.status_code == 404


def test_verify_roundtrip_and_tampered_pack_fails():
    store.add_person("verify-owner", "Mia", MIA)
    post_signal("verify-owner", "first entry")
    post_signal("verify-owner", "second entry")
    evidence.run_anchor(submit_fn=_ok_submit)

    pack = _pack_for("verify-owner")
    r = client.post("/evidence/verify", json=pack)
    assert r.status_code == 200
    body = r.json()
    assert body["chain_intact"] is True
    assert body["entries_checked"] == 2
    assert body["anchor_status"] == "anchored"

    pack["entries"][0]["snippet"] = "edited after the fact"
    bad = client.post("/evidence/verify", json=pack).json()
    assert bad["chain_intact"] is False

    malformed = client.post("/evidence/verify", json={"nope": 1}).json()
    assert malformed == {
        "chain_intact": False,
        "entries_checked": 0,
        "anchor_status": "none",
    }


def test_verify_page_loads():
    r = client.get("/verify")
    assert r.status_code == 200
    assert "evidence pack" in r.text
    assert "/evidence/verify" in r.text


# ---------------------------------------------------------------------------
# Forget me + the agent command (through the real WhatsApp flow)
# ---------------------------------------------------------------------------


def test_forget_me_removes_chained_rows():
    store.add_person("forget-owner", "Peter", PETER)
    store.add_signal("forget-owner", "whatsapp", "c1", PETER, "one", "2026-11-01T10:00:00")
    store.add_signal("forget-owner", "whatsapp", "c1", PETER, "two", "2026-11-01T10:01:00")
    before = evidence.verify_chain("forget-owner", PETER_HASH)
    assert before["entries"] == 2
    assert before["chained_entries"] == 2

    store.delete_owner_data("forget-owner")
    assert store.signals_for_stream("forget-owner", PETER_HASH) == []
    after = evidence.verify_chain("forget-owner", PETER_HASH)
    assert after["entries"] == 0
    assert after["chained_entries"] == 0


_counter = [7000]


def wa_payload(wa_id, name, message):
    _counter[0] += 1
    message.setdefault("id", f"wamid.ev{_counter[0]}")
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


def wa_send(wa_id, name, message):
    r = client.post("/webhooks/whatsapp", json=wa_payload(wa_id, name, message))
    assert r.status_code == 200, r.text
    return [rec for rec in wa.DRY_RUN_LOG if rec["to"] == wa_id]


def test_forward_chains_and_agent_evidence_command():
    wa_send(SARAH, "Sarah", {"type": "text", "text": {"body": "hi"}})
    wa_send(SARAH, "Sarah", {"type": "text", "text": {"body": "YES"}})
    wa_send(
        SARAH,
        "Sarah",
        {
            "type": "contacts",
            "contacts": [
                {
                    "name": {"formatted_name": "Peter"},
                    "phones": [{"phone": "+1 555-010-7777", "wa_id": PETER_WA}],
                }
            ],
        },
    )
    wa_send(
        SARAH,
        "Sarah",
        {"type": "text", "text": {"body": "hey you"}, "context": {"forwarded": True}},
    )
    owner = store.owner_id_for_address("whatsapp", SARAH)
    assert_stream_chained(owner, PETER_HASH, 1)

    wa.DRY_RUN_LOG.clear()
    wa_send(SARAH, "Sarah", {"type": "text", "text": {"body": "evidence"}})
    replies = [rec["text"] for rec in wa.DRY_RUN_LOG if rec["to"] == SARAH]
    assert any("Your evidence" in t and "Peter" in t for t in replies)
    assert any("chain intact ✓" in t for t in replies)
    assert any("timestamp anchor pending" in t for t in replies)

    wa.DRY_RUN_LOG.clear()
    wa_send(SARAH, "Sarah", {"type": "text", "text": {"body": "proof"}})
    replies = [rec["text"] for rec in wa.DRY_RUN_LOG if rec["to"] == SARAH]
    assert any("Your evidence" in t for t in replies)
