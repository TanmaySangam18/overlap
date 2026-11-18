"""SQLite storage for OVERLAP signals, reviews, and alerts.

Privacy notes for the prototype:
- Sender identifiers are stored only as a SHA-256 hash plus a masked
  display form (see normalize.py). The raw identifier is never persisted.
- Message text is stored only as a short snippet (first 80 characters),
  which is the most the API ever returns as evidence.
"""

import os
import secrets
import sqlite3
from contextlib import contextmanager

SNIPPET_LEN = 80

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    source TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    identifier_type TEXT NOT NULL,
    identifier_hash TEXT NOT NULL,
    identifier_masked TEXT NOT NULL,
    message_snippet TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_signals_hash ON signals(identifier_hash);
CREATE INDEX IF NOT EXISTS idx_signals_owner ON signals(owner_id);

CREATE TABLE IF NOT EXISTS reviews (
    match_key TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('confirmed', 'dismissed')),
    reviewed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (match_key, owner_id)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_key TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    channel TEXT NOT NULL DEFAULT 'internal_only'
);

CREATE TABLE IF NOT EXISTS ingest_tokens (
    token TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Evidence anchoring: one row per root-hash submission to a public
-- timestamp calendar (see app/evidence.py). 'pending' means the root is
-- recorded here but the calendar has not accepted it yet; later runs
-- retry. 'proof' holds the calendar's returned timestamp proof (hex).
CREATE TABLE IF NOT EXISTS anchor_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL DEFAULT 'pending',
    proof TEXT
);

-- Chat-agent (bot) tables: the agent lives inside WhatsApp / Messages, so
-- a chat user is identified by their channel address. Their owner_id is
-- derived from a hash of that address; the raw address is kept only so the
-- bot can reply to them (it is their own address, given by messaging us).
CREATE TABLE IF NOT EXISTS chat_users (
    owner_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    address TEXT NOT NULL,
    display_name TEXT,
    consented_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- "Once per person, ever": people a user has registered via a contact
-- card. Only the hash + masked form of the number is stored, exactly like
-- signal identifiers.
CREATE TABLE IF NOT EXISTS persons (
    owner_id TEXT NOT NULL,
    label TEXT NOT NULL,
    identifier_type TEXT NOT NULL,
    identifier_hash TEXT NOT NULL,
    identifier_masked TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (owner_id, identifier_hash)
);

CREATE TABLE IF NOT EXISTS agent_state (
    owner_id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'idle',
    context TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Both directions of a pair's conversation (exports contain the owner's
-- own messages too). Signals only ever hold incoming messages; pair
-- messages are what relationship detection reads.
CREATE TABLE IF NOT EXISTS pair_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    partner_hash TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    message_snippet TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'whatsapp'
);
CREATE INDEX IF NOT EXISTS idx_pair_owner_partner
    ON pair_messages(owner_id, partner_hash);

CREATE TABLE IF NOT EXISTS pair_state (
    owner_id TEXT NOT NULL,
    partner_hash TEXT NOT NULL,
    relationship_state TEXT,
    since_iso TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (owner_id, partner_hash)
);

-- Dedupe keys: webhook message ids (retries) and export message keys
-- (re-exported history must not be ingested twice).
CREATE TABLE IF NOT EXISTS ingested_keys (
    owner_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (owner_id, dedupe_key)
);

-- One-shot notification log so a match/flag is announced once, not on
-- every new signal.
CREATE TABLE IF NOT EXISTS notice_log (
    owner_id TEXT NOT NULL,
    notice_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (owner_id, notice_key)
);

-- Messages the agent owes other users (e.g. a match notice for someone
-- who is not the user currently chatting). Transports drain this.
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    address TEXT NOT NULL,
    text TEXT NOT NULL,
    buttons TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at TEXT
);
"""


def _db_path() -> str:
    return os.environ.get("OVERLAP_DB_PATH", "overlap.db")


@contextmanager
def _connect():
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """Additive migrations for databases created before a column existed.

    The signals table gained its evidence-chain columns (prev_hash,
    chain_hash — see app/evidence.py) after launch. Rows written before
    that keep NULLs and form the chain's documented "legacy" segment:
    counted and listed, but not hash-verified.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(signals)")}
    if "prev_hash" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN prev_hash TEXT")
    if "chain_hash" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN chain_hash TEXT")


def add_signal(owner_id, source, conversation_id, ident, message_text, sent_at_iso):
    """Store one signal — the single funnel every ingest path uses.

    Each signal is hash-chained to the previous signal in the same
    (owner, person) stream (see app/evidence.py): the row stores
    prev_hash (the previous entry's hash, or "GENESIS") and chain_hash
    (the hash of this entry). received_at is generated here rather than
    by the SQLite default because it is one of the hashed fields — the
    value inside the hash must be exactly the value stored. The format
    matches SQLite's datetime('now'): "YYYY-MM-DD HH:MM:SS", UTC.
    """
    from datetime import datetime, timezone

    from .evidence import compute_entry_hash

    snippet = (message_text or "")[:SNIPPET_LEN]
    received_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        tip = conn.execute(
            "SELECT chain_hash FROM signals WHERE owner_id = ? "
            "AND identifier_hash = ? AND chain_hash IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (owner_id, ident["hash"]),
        ).fetchone()
        prev_hash = tip["chain_hash"] if tip else "GENESIS"
        chain_hash = compute_entry_hash(
            prev_hash, owner_id, ident["hash"], source, snippet, received_at
        )
        cur = conn.execute(
            """
            INSERT INTO signals (owner_id, source, conversation_id,
                                 identifier_type, identifier_hash,
                                 identifier_masked, message_snippet, sent_at,
                                 received_at, prev_hash, chain_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_id,
                source,
                conversation_id,
                ident["type"],
                ident["hash"],
                ident["masked"],
                snippet,
                sent_at_iso,
                received_at,
                prev_hash,
                chain_hash,
            ),
        )
        return cur.lastrowid


def signals_for_hash(identifier_hash):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE identifier_hash = ? ORDER BY sent_at",
            (identifier_hash,),
        ).fetchall()
    return [dict(r) for r in rows]


def hashes_for_owner(owner_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT identifier_hash FROM signals WHERE owner_id = ?",
            (owner_id,),
        ).fetchall()
    return [r["identifier_hash"] for r in rows]


def signals_for_stream(owner_id, identifier_hash):
    """One owner's signals about one person, in insertion (chain) order."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE owner_id = ? AND identifier_hash = ? "
            "ORDER BY id",
            (owner_id, identifier_hash),
        ).fetchall()
    return [dict(r) for r in rows]


def all_chain_tips():
    """The latest chain_hash of every (owner, person) stream that has one."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT owner_id, identifier_hash, chain_hash FROM signals "
            "WHERE chain_hash IS NOT NULL ORDER BY id"
        ).fetchall()
    tips = {}
    for row in rows:
        tips[(row["owner_id"], row["identifier_hash"])] = row["chain_hash"]
    return list(tips.values())


def add_anchor_record(root_hash):
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO anchor_records (root_hash, status) VALUES (?, 'pending')",
            (root_hash,),
        )
        return cur.lastrowid


def update_anchor_record(record_id, status, proof=None):
    with _connect() as conn:
        conn.execute(
            "UPDATE anchor_records SET status = ?, proof = ? WHERE id = ?",
            (status, proof, record_id),
        )


def anchor_records():
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM anchor_records ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def pending_anchor_records():
    return [r for r in anchor_records() if r["status"] == "pending"]


def latest_anchor_record():
    records = anchor_records()
    return records[-1] if records else None


def find_hash_by_match_id(match_id):
    """match_id is 'match_' + first 16 hex chars of the identifier hash."""
    if not match_id.startswith("match_"):
        return None
    prefix = match_id[len("match_"):]
    with _connect() as conn:
        row = conn.execute(
            "SELECT identifier_hash FROM signals WHERE substr(identifier_hash, 1, ?) = ? LIMIT 1",
            (len(prefix), prefix),
        ).fetchone()
    return row["identifier_hash"] if row else None


def set_review(match_key, owner_id, decision):
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO reviews (match_key, owner_id, decision)
            VALUES (?, ?, ?)
            ON CONFLICT(match_key, owner_id)
            DO UPDATE SET decision = excluded.decision,
                          reviewed_at = datetime('now')
            """,
            (match_key, owner_id, decision),
        )


def get_review(match_key, owner_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM reviews WHERE match_key = ? AND owner_id = ?",
            (match_key, owner_id),
        ).fetchone()
    return dict(row) if row else None


def add_alert(match_key, owner_id):
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (match_key, created_by) VALUES (?, ?)",
            (match_key, owner_id),
        )
        return cur.lastrowid


def create_ingest_token(owner_id):
    """Issue a personal ingest token (used by the iOS Shortcuts automation).

    Creating a token is the recorded consent grant for automated submission:
    the user builds the automation on their own phone and pastes this token
    into it. Anyone holding the token can submit as that user, so it is
    treated like a password and never logged.
    """
    token = "ovl_" + secrets.token_urlsafe(24)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO ingest_tokens (token, owner_id) VALUES (?, ?)",
            (token, owner_id),
        )
    return token


def owner_for_token(token):
    if not token:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT owner_id FROM ingest_tokens WHERE token = ?",
            (token,),
        ).fetchone()
    return row["owner_id"] if row else None


# ---------------------------------------------------------------------------
# Chat-agent storage
# ---------------------------------------------------------------------------

import hashlib as _hashlib
import json as _json


def owner_id_for_address(channel: str, address: str) -> str:
    """Deterministic owner id for a chat user, derived from their address."""
    digest = _hashlib.sha256(f"{channel}:{address}".encode("utf-8")).hexdigest()
    return "u_" + digest[:16]


def get_or_create_chat_user(channel, address, display_name=None):
    owner_id = owner_id_for_address(channel, address)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM chat_users WHERE owner_id = ?", (owner_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO chat_users (owner_id, channel, address, display_name) "
                "VALUES (?, ?, ?, ?)",
                (owner_id, channel, address, display_name),
            )
            row = conn.execute(
                "SELECT * FROM chat_users WHERE owner_id = ?", (owner_id,)
            ).fetchone()
        elif display_name and row["display_name"] != display_name:
            conn.execute(
                "UPDATE chat_users SET display_name = ? WHERE owner_id = ?",
                (display_name, owner_id),
            )
            row = conn.execute(
                "SELECT * FROM chat_users WHERE owner_id = ?", (owner_id,)
            ).fetchone()
    return dict(row)


def get_chat_user(owner_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM chat_users WHERE owner_id = ?", (owner_id,)
        ).fetchone()
    return dict(row) if row else None


def set_consent(owner_id):
    with _connect() as conn:
        conn.execute(
            "UPDATE chat_users SET consented_at = datetime('now') WHERE owner_id = ?",
            (owner_id,),
        )


def set_display_name(owner_id, display_name):
    with _connect() as conn:
        conn.execute(
            "UPDATE chat_users SET display_name = ? WHERE owner_id = ?",
            (display_name, owner_id),
        )


def add_person(owner_id, label, ident):
    """Register a person for this owner ('once per person, ever').

    Returns True if newly registered, False if this person was already
    registered (same hashed identifier).
    """
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO persons "
            "(owner_id, label, identifier_type, identifier_hash, identifier_masked) "
            "VALUES (?, ?, ?, ?, ?)",
            (owner_id, label, ident["type"], ident["hash"], ident["masked"]),
        )
        return cur.rowcount > 0


def persons_for_owner(owner_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM persons WHERE owner_id = ? ORDER BY created_at",
            (owner_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def person_by_hash(owner_id, identifier_hash):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM persons WHERE owner_id = ? AND identifier_hash = ?",
            (owner_id, identifier_hash),
        ).fetchone()
    return dict(row) if row else None


def person_by_label(owner_id, label):
    label = (label or "").strip().lower()
    for person in persons_for_owner(owner_id):
        if person["label"].strip().lower() == label:
            return person
    return None


def get_agent_state(owner_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM agent_state WHERE owner_id = ?", (owner_id,)
        ).fetchone()
    if row is None:
        return {"state": "idle", "context": {}}
    return {"state": row["state"], "context": _json.loads(row["context"] or "{}")}


def set_agent_state(owner_id, state, context):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO agent_state (owner_id, state, context) VALUES (?, ?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET state = excluded.state, "
            "context = excluded.context, updated_at = datetime('now')",
            (owner_id, state, _json.dumps(context or {})),
        )


def add_pair_message(owner_id, partner_hash, direction, message_text, sent_at_iso, source):
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO pair_messages "
            "(owner_id, partner_hash, direction, message_snippet, sent_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                owner_id,
                partner_hash,
                direction,
                (message_text or "")[:SNIPPET_LEN],
                sent_at_iso,
                source,
            ),
        )
        return cur.lastrowid


def pair_messages_for(owner_id, partner_hash):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pair_messages WHERE owner_id = ? AND partner_hash = ? "
            "ORDER BY sent_at",
            (owner_id, partner_hash),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pair_state(owner_id, partner_hash):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pair_state WHERE owner_id = ? AND partner_hash = ?",
            (owner_id, partner_hash),
        ).fetchone()
    return dict(row) if row else None


def set_pair_state(owner_id, partner_hash, relationship_state, since_iso):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO pair_state (owner_id, partner_hash, relationship_state, since_iso) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(owner_id, partner_hash) DO UPDATE SET "
            "relationship_state = excluded.relationship_state, "
            "since_iso = excluded.since_iso, updated_at = datetime('now')",
            (owner_id, partner_hash, relationship_state, since_iso),
        )


def mark_ingested(owner_id, dedupe_key, kind=""):
    """Record a dedupe key. Returns True if it was new, False if seen."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO ingested_keys (owner_id, dedupe_key, kind) "
            "VALUES (?, ?, ?)",
            (owner_id, dedupe_key, kind),
        )
        return cur.rowcount > 0


def release_ingested(owner_id, dedupe_key):
    """Undo a dedupe mark after a failed processing attempt.

    Webhook dedupe is recorded before the agent runs (so a platform
    retry of a *successful* delivery is a no-op). If processing itself
    raised, the message was never actually handled — releasing the key
    lets the platform's retry process it for real instead of the retry
    being swallowed as a duplicate.
    """
    with _connect() as conn:
        conn.execute(
            "DELETE FROM ingested_keys WHERE owner_id = ? AND dedupe_key = ?",
            (owner_id, dedupe_key),
        )


def recent_signals(owner_id, limit=5):
    """The owner's most recently stored signals, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE owner_id = ? ORDER BY id DESC LIMIT ?",
            (owner_id, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_last_signal(owner_id, identifier_hash=None):
    """Delete the owner's most recent signal (the agent's 'undo').

    Only the newest entry of its (owner, person) stream is ever removed:
    that entry is the stream's chain tip, so deleting it leaves the
    remaining evidence chain intact (deleting a middle entry would
    break the link after it, by design). The matching pair message —
    the same snippet, time and direction written alongside the signal —
    is removed with it. Returns the deleted signal row, or None.
    """
    query = "SELECT * FROM signals WHERE owner_id = ?"
    params = [owner_id]
    if identifier_hash:
        query += " AND identifier_hash = ?"
        params.append(identifier_hash)
    query += " ORDER BY id DESC LIMIT 1"
    with _connect() as conn:
        row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM signals WHERE id = ?", (row["id"],))
        pair = conn.execute(
            "SELECT id FROM pair_messages WHERE owner_id = ? "
            "AND partner_hash = ? AND direction = 'in' "
            "AND message_snippet = ? AND sent_at = ? "
            "ORDER BY id DESC LIMIT 1",
            (
                owner_id,
                row["identifier_hash"],
                row["message_snippet"],
                row["sent_at"],
            ),
        ).fetchone()
        if pair is not None:
            conn.execute("DELETE FROM pair_messages WHERE id = ?", (pair["id"],))
    return dict(row)


def add_notice(owner_id, notice_key):
    """Record that a notice was sent. Returns True the first time only."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO notice_log (owner_id, notice_key) VALUES (?, ?)",
            (owner_id, notice_key),
        )
        return cur.rowcount > 0


def enqueue_outbox(owner_id, channel, address, text, buttons=None):
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO outbox (owner_id, channel, address, text, buttons) "
            "VALUES (?, ?, ?, ?, ?)",
            (owner_id, channel, address, text, _json.dumps(buttons or [])),
        )
        return cur.lastrowid


def peek_outbox(channel=None, owner_id=None):
    """Pending outbox rows WITHOUT marking them sent.

    Transports use peek + mark_outbox_sent so a failed send leaves the
    notice queued for the next delivery instead of losing it.
    """
    query = "SELECT * FROM outbox WHERE sent_at IS NULL"
    params = []
    if channel:
        query += " AND channel = ?"
        params.append(channel)
    if owner_id:
        query += " AND owner_id = ?"
        params.append(owner_id)
    query += " ORDER BY id"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["buttons"] = _json.loads(item["buttons"] or "[]")
        out.append(item)
    return out


def mark_outbox_sent(row_ids):
    """Mark outbox rows delivered — call only after the send succeeded."""
    if not row_ids:
        return
    with _connect() as conn:
        conn.executemany(
            "UPDATE outbox SET sent_at = datetime('now') WHERE id = ?",
            [(row_id,) for row_id in row_ids],
        )


def drain_outbox(channel=None, owner_id=None):
    """Fetch and mark-sent pending outbox rows (optionally filtered)."""
    out = peek_outbox(channel=channel, owner_id=owner_id)
    mark_outbox_sent([row["id"] for row in out])
    return out


def delete_owner_data(owner_id):
    """Full self-deletion for a chat user ('forget me')."""
    with _connect() as conn:
        for table in (
            "signals",
            "persons",
            "pair_messages",
            "pair_state",
            "agent_state",
            "ingested_keys",
            "notice_log",
            "outbox",
        ):
            conn.execute(f"DELETE FROM {table} WHERE owner_id = ?", (owner_id,))
        conn.execute("DELETE FROM reviews WHERE owner_id = ?", (owner_id,))
        conn.execute("DELETE FROM alerts WHERE created_by = ?", (owner_id,))
        conn.execute("DELETE FROM chat_users WHERE owner_id = ?", (owner_id,))


# ---------------------------------------------------------------------------
# Operations: health + retention
# ---------------------------------------------------------------------------

from datetime import datetime as _datetime
from datetime import timedelta as _timedelta
from datetime import timezone as _timezone


def db_reachable() -> bool:
    """True if the SQLite database answers a trivial query."""
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
        return True
    except sqlite3.Error:
        return False


def retention_days_from_env() -> int:
    """OVERLAP_RETENTION_DAYS; 0 (the default) means keep forever."""
    try:
        return max(0, int(os.environ.get("OVERLAP_RETENTION_DAYS", "0")))
    except ValueError:
        return 0


def purge_old(retention_days=None):
    """Delete snippet-bearing rows older than the retention window.

    Removes old rows from the two tables that store message snippets
    (signals and pair_messages). Registrations, reviews, and pair state
    are kept: they carry no message text, and a pair's baseline summary
    must survive its oldest messages. Nothing calls this automatically —
    it is an operator action (see DEPLOY-PROD.md), run on whatever
    schedule the operator chooses.

    retention_days=None reads OVERLAP_RETENTION_DAYS. A value <= 0 means
    keep forever and purges nothing. Returns the per-table delete counts.
    """
    if retention_days is None:
        retention_days = retention_days_from_env()
    if retention_days <= 0:
        return {"signals": 0, "pair_messages": 0}
    cutoff = (
        _datetime.now(_timezone.utc).replace(tzinfo=None)
        - _timedelta(days=int(retention_days))
    ).isoformat()
    with _connect() as conn:
        signals = conn.execute(
            "DELETE FROM signals WHERE sent_at < ?", (cutoff,)
        ).rowcount
        pair_messages = conn.execute(
            "DELETE FROM pair_messages WHERE sent_at < ?", (cutoff,)
        ).rowcount
    return {"signals": signals, "pair_messages": pair_messages}
