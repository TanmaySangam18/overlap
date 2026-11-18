"""Owner-scoped API authorization (P0 fix, 2026-11-18).

Before this fix, /signals, /matches and /tokens were unauthenticated
and owner ids are derivable (chat owner ids are a truncated hash of
channel:address), so anyone who knew a user's phone number could read
their match evidence and mint credentials for their account. These
tests use the RAW Starlette client — no operator header is added for
them (see conftest.py) — and pin the new behavior:

- no credential -> 401 on every owner-scoped endpoint
- wrong credential -> 401
- another owner's ingest token -> 401
- the owner's own ingest token -> works
- the operator token -> works; /tokens is operator-only
- /docs and /openapi.json are not served outside OVERLAP_ENV=dev
"""

import os

import pytest
from starlette.testclient import TestClient

from app import store
from app.main import app
from app.normalize import normalize_identifier

client = TestClient(app)
ADMIN = {"X-Overlap-Admin": "test-operator-token"}
WRONG = {"X-Overlap-Admin": "not-the-operator-token"}

SHARED = "+1 555-010-7777"
SHARED_HASH = normalize_identifier(SHARED)["hash"]
MATCH_ID = f"match_{SHARED_HASH[:16]}"

SIGNAL = {
    "source": "whatsapp",
    "conversation_id": "chat-1",
    "sender_identifier": SHARED,
    "message_text": "see you tonight",
    "sent_at": "2026-11-17T14:30:00Z",
    "consent_confirmed": True,
}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    os.environ["OVERLAP_DB_PATH"] = str(tmp_path / "auth-test.db")
    yield


def seed_match():
    ident = normalize_identifier(SHARED)
    for owner in ("alice", "bob"):
        store.add_signal(
            owner_id=owner,
            source="whatsapp",
            conversation_id="chat-1",
            ident=ident,
            message_text="see you tonight",
            sent_at_iso="2026-11-17T14:30:00",
        )


def test_matches_list_requires_owner_credential():
    seed_match()
    assert client.get("/matches", params={"owner_id": "alice"}).status_code == 401
    assert (
        client.get("/matches", params={"owner_id": "alice"}, headers=WRONG).status_code
        == 401
    )
    bob_token = store.create_ingest_token("bob")
    assert (
        client.get(
            "/matches",
            params={"owner_id": "alice"},
            headers={"X-Overlap-Token": bob_token},
        ).status_code
        == 401
    )
    alice_token = store.create_ingest_token("alice")
    ok = client.get(
        "/matches",
        params={"owner_id": "alice"},
        headers={"X-Overlap-Token": alice_token},
    )
    assert ok.status_code == 200
    assert ok.json()["matches"][0]["match_id"] == MATCH_ID
    assert client.get("/matches", params={"owner_id": "alice"}, headers=ADMIN).status_code == 200


def test_match_detail_review_alert_require_auth():
    seed_match()
    assert (
        client.get(f"/matches/{MATCH_ID}", params={"owner_id": "alice"}).status_code
        == 401
    )
    assert (
        client.post(
            f"/matches/{MATCH_ID}/review",
            json={"owner_id": "alice", "decision": "confirmed"},
        ).status_code
        == 401
    )
    assert (
        client.post(f"/matches/{MATCH_ID}/alert", json={"owner_id": "alice"}).status_code
        == 401
    )


def test_signals_require_owner_credential():
    body = {**SIGNAL, "owner_id": "alice"}
    assert client.post("/signals", json=body).status_code == 401
    assert client.post("/signals", json=body, headers=WRONG).status_code == 401
    alice_token = store.create_ingest_token("alice")
    ok = client.post(
        "/signals", json=body, headers={"X-Overlap-Token": alice_token}
    )
    assert ok.status_code == 201


def test_token_minting_is_operator_only():
    assert client.post("/tokens", json={"owner_id": "alice"}).status_code == 401
    assert (
        client.post("/tokens", json={"owner_id": "alice"}, headers=WRONG).status_code
        == 401
    )
    alice_token = store.create_ingest_token("alice")
    assert (
        client.post(
            "/tokens",
            json={"owner_id": "alice"},
            headers={"X-Overlap-Token": alice_token},
        ).status_code
        == 401
    )
    ok = client.post("/tokens", json={"owner_id": "alice"}, headers=ADMIN)
    assert ok.status_code == 201
    assert ok.json()["ingest_token"]


def test_docs_are_not_served_by_default():
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
