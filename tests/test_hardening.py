"""Production-hardening tests: webhook signatures, rate limits, legal
pages, health/headers, upload cap, retention purge, DB backup, prod mode.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ratelimit, store, whatsapp as wa
from app.main import app
from app.normalize import normalize_identifier

client = TestClient(app)
PROJECT = Path(__file__).resolve().parent.parent
SECRET = "test-app-secret"


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    os.environ["OVERLAP_DB_PATH"] = str(tmp_path / "hardening-test.db")
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    monkeypatch.delenv("OVERLAP_RATE_LIMIT_OFF", raising=False)
    monkeypatch.delenv("OVERLAP_RATE_LIMIT_PER_MIN", raising=False)
    ratelimit.reset()
    wa.DRY_RUN_LOG.clear()
    yield


# ---------------------------------------------------------------------------
# WhatsApp webhook signature verification
# ---------------------------------------------------------------------------


def _wa_raw(wa_id="15559990001", body="hi"):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [
                                {"wa_id": wa_id, "profile": {"name": "Tester"}}
                            ],
                            "messages": [
                                {
                                    "id": "wamid.sig1",
                                    "from": wa_id,
                                    "timestamp": "1767225600",
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }
    return json.dumps(payload).encode()


def _sign(secret, raw):
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_webhook_signature_valid_passes(monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    raw = _wa_raw()
    r = client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(SECRET, raw),
        },
    )
    assert r.status_code == 200


def test_webhook_signature_invalid_or_missing_rejected(monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    raw = _wa_raw()
    wrong = client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign("some-other-secret", raw),
        },
    )
    assert wrong.status_code == 403
    missing = client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert missing.status_code == 403
    # Nothing was processed.
    assert wa.DRY_RUN_LOG == []


def test_webhook_signature_unset_secret_still_works():
    raw = _wa_raw()
    r = client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limit_trips_for_one_sender(monkeypatch):
    monkeypatch.setenv("OVERLAP_RATE_LIMIT_PER_MIN", "3")
    ratelimit.reset()
    number = "+15559998888"
    codes = [
        client.post(
            "/webhooks/sms",
            data={"From": number, "Body": "hi", "MessageSid": f"SM-rl-{i}"},
        ).status_code
        for i in range(4)
    ]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429
    # A different sender is unaffected.
    other = client.post(
        "/webhooks/sms",
        data={"From": "+15559997777", "Body": "hi", "MessageSid": "SM-rl-other"},
    )
    assert other.status_code == 200


def test_rate_limit_kill_switch(monkeypatch):
    monkeypatch.setenv("OVERLAP_RATE_LIMIT_PER_MIN", "1")
    monkeypatch.setenv("OVERLAP_RATE_LIMIT_OFF", "1")
    ratelimit.reset()
    for i in range(3):
        r = client.post(
            "/webhooks/sms",
            data={"From": "+15559996666", "Body": "hi", "MessageSid": f"SM-off-{i}"},
        )
        assert r.status_code == 200


def test_ingest_rate_limit_trips(monkeypatch):
    monkeypatch.setenv("OVERLAP_RATE_LIMIT_PER_MIN", "2")
    ratelimit.reset()
    token = client.post("/tokens", json={"owner_id": "rl-owner"}).json()[
        "ingest_token"
    ]
    body = {"sender_identifier": "+1 555-010-7777", "message_text": "hey"}
    codes = [
        client.post(
            "/ingest/shortcut", json=body, headers={"X-Overlap-Token": token}
        ).status_code
        for _ in range(3)
    ]
    assert codes == [201, 201, 429]


# ---------------------------------------------------------------------------
# Legal pages, health, headers
# ---------------------------------------------------------------------------


def test_privacy_and_terms_pages():
    p = client.get("/privacy")
    assert p.status_code == 200
    assert "SHA-256" in p.text
    assert "80 characters" in p.text
    assert "forget me" in p.text
    assert "never read a chat you did not send" in p.text
    assert "not proof of a relationship or misconduct" in p.text
    t = client.get("/terms")
    assert t.status_code == 200
    assert "beta" in t.text.lower()
    assert "not proof of a relationship or misconduct" in t.text


def test_footer_links_point_to_legal_pages():
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="/privacy"' in r.text
    assert 'href="/terms"' in r.text


def test_healthz_and_security_headers():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok"}
    home = client.get("/")
    assert home.headers["X-Content-Type-Options"] == "nosniff"
    assert home.headers["X-Frame-Options"] == "DENY"
    assert home.headers["Referrer-Policy"] == "no-referrer"


def test_export_file_over_10mb_rejected():
    token = client.post("/tokens", json={"owner_id": "big-upload"}).json()[
        "ingest_token"
    ]
    big = b"x" * (10 * 1024 * 1024 + 1)
    r = client.post(
        "/ingest/export-file",
        files={"file": ("chat.txt", big, "text/plain")},
        data={"partner_identifier": "+1 555-010-7777", "owner_name": "Sarah"},
        headers={"X-Overlap-Token": token},
    )
    assert r.status_code == 413


# ---------------------------------------------------------------------------
# Retention purge
# ---------------------------------------------------------------------------


def test_purge_old_respects_window_and_zero():
    ident = normalize_identifier("+1 555-010-7777")
    store.add_signal(
        "purge-owner", "whatsapp", "c1", ident, "old hello", "2020-01-01T00:00:00"
    )
    store.add_signal(
        "purge-owner", "whatsapp", "c1", ident, "fresh hello", "2999-01-01T00:00:00"
    )
    store.add_pair_message(
        "purge-owner", ident["hash"], "in", "old pair", "2020-06-01T00:00:00", "whatsapp"
    )
    store.add_pair_message(
        "purge-owner", ident["hash"], "in", "new pair", "2999-06-01T00:00:00", "whatsapp"
    )
    # 0 = keep forever.
    assert store.purge_old(0) == {"signals": 0, "pair_messages": 0}
    result = store.purge_old(30)
    assert result == {"signals": 1, "pair_messages": 1}
    remaining = store.signals_for_hash(ident["hash"])
    assert [s["message_snippet"] for s in remaining] == ["fresh hello"]
    pairs = store.pair_messages_for("purge-owner", ident["hash"])
    assert [m["message_snippet"] for m in pairs] == ["new pair"]


def test_purge_old_defaults_to_env(monkeypatch):
    ident = normalize_identifier("+1 555-010-7777")
    store.add_signal(
        "purge-env", "whatsapp", "c1", ident, "ancient", "2020-01-01T00:00:00"
    )
    monkeypatch.delenv("OVERLAP_RETENTION_DAYS", raising=False)
    assert store.purge_old() == {"signals": 0, "pair_messages": 0}
    monkeypatch.setenv("OVERLAP_RETENTION_DAYS", "30")
    assert store.purge_old() == {"signals": 1, "pair_messages": 0}


# ---------------------------------------------------------------------------
# Backup script + prod mode (subprocess: env is read at import time)
# ---------------------------------------------------------------------------


def test_backup_script_snapshots_db(tmp_path):
    db = tmp_path / "prod.db"
    os.environ["OVERLAP_DB_PATH"] = str(db)
    ident = normalize_identifier("+1 555-010-7777")
    store.add_signal(
        "backup-owner", "whatsapp", "c1", ident, "hello", "2026-01-01T00:00:00"
    )
    backups = tmp_path / "backups"
    env = dict(
        os.environ,
        OVERLAP_DB_PATH=str(db),
        OVERLAP_BACKUP_DIR=str(backups),
    )
    r = subprocess.run(
        [sys.executable, "scripts/backup_db.py"],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    snaps = list(backups.glob("overlap-*.db"))
    assert len(snaps) == 1
    conn = sqlite3.connect(snaps[0])
    try:
        count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_prod_mode_disables_docs(tmp_path):
    env = dict(
        os.environ,
        OVERLAP_ENV="prod",
        OVERLAP_DB_PATH=str(tmp_path / "prod-mode.db"),
    )
    code = (
        "from fastapi.testclient import TestClient;"
        "from app.main import app;"
        "c = TestClient(app);"
        "assert c.get('/docs').status_code == 404;"
        "assert c.get('/openapi.json').status_code == 404;"
        "assert c.get('/healthz').status_code == 200;"
        "print('prod-ok')"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "prod-ok" in r.stdout


def test_dev_mode_docs_available(tmp_path):
    # Docs are served ONLY under an explicit OVERLAP_ENV=dev; the
    # default environment (this test process) must not serve them.
    assert client.get("/docs").status_code == 404
    env = dict(
        os.environ,
        OVERLAP_ENV="dev",
        OVERLAP_DB_PATH=str(tmp_path / "dev-mode.db"),
    )
    code = (
        "from fastapi.testclient import TestClient;"
        "from app.main import app;"
        "c = TestClient(app);"
        "assert c.get('/docs').status_code == 200;"
        "assert c.get('/openapi.json').status_code == 200;"
        "print('dev-ok')"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "dev-ok" in r.stdout
