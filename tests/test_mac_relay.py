"""Mac relay: Apple-epoch conversion, chat.db reading (both directions,
group-chat and empty-message filtering), payload shape, and posting.

Uses a fake chat.db built in tmp_path — no Mac and no real Messages
database needed.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts import mac_relay  # noqa: E402

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _ns(dt):
    return int((dt - APPLE_EPOCH).total_seconds() * 1e9)


def make_chat_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT,
            handle_id INTEGER, is_from_me INTEGER, date INTEGER,
            item_type INTEGER, cache_has_attachments INTEGER
        );
        """
    )
    # Mia: +15550108888, handle 1, 1:1 chat 1. Group chat 2 has Mia + Sam.
    conn.execute("INSERT INTO handle VALUES (1, '+15550108888', 'iMessage')")
    conn.execute("INSERT INTO handle VALUES (2, '+15550109999', 'iMessage')")
    conn.execute("INSERT INTO chat VALUES (1, 'iMessage;-;+15550108888')")
    conn.execute("INSERT INTO chat VALUES (2, 'iMessage;-;+15550108888;+15550109999')")
    conn.execute("INSERT INTO chat_handle_join VALUES (1, 1)")
    conn.execute("INSERT INTO chat_handle_join VALUES (2, 1)")
    conn.execute("INSERT INTO chat_handle_join VALUES (2, 2)")
    t0 = datetime(2026, 11, 17, 23, 42, tzinfo=timezone.utc)
    rows = [
        # ROWID, guid, text, handle_id, is_from_me, date, item_type
        (1, "g1", "i loved last night", 1, 0, _ns(t0), 0),
        (2, "g2", "goodnight", 1, 1, _ns(t0), 0),  # sent, 1:1 -> partner Mia
        (3, "g3", "hey all", 1, 1, _ns(t0), 0),  # sent, group -> skipped
        (4, "g4", "", 1, 0, _ns(t0), 0),  # empty text -> skipped
        (5, "g5", "renamed the chat", 1, 0, _ns(t0), 1),  # not a message
        (6, "g6", "see you tomorrow", 2, 0, _ns(t0), 0),  # incoming, Sam
    ]
    for r in rows:
        conn.execute("INSERT INTO message VALUES (?,?,?,?,?,?,?,0)", r)
    for mid, chat_id in [(1, 1), (2, 1), (3, 2), (4, 1), (5, 1), (6, 2)]:
        conn.execute("INSERT INTO chat_message_join VALUES (?,?)", (chat_id, mid))
    conn.commit()
    conn.close()


def test_apple_epoch_nanoseconds_and_seconds():
    t = datetime(2026, 11, 17, 23, 42, tzinfo=timezone.utc)
    assert mac_relay.apple_to_datetime(_ns(t)) == t
    assert mac_relay.apple_to_datetime(int((t - APPLE_EPOCH).total_seconds())) == t


def test_incoming_only_by_default(tmp_path):
    db = str(tmp_path / "chat.db")
    make_chat_db(db)
    messages, last = mac_relay.read_new_messages(db, 0, include_sent=False)
    assert [m["rowid"] for m in messages] == [1, 6]
    assert messages[0]["sender_identifier"] == "+15550108888"
    assert messages[0]["message_text"] == "i loved last night"
    assert messages[0]["sent_at"] == datetime(2026, 11, 17, 23, 42, tzinfo=timezone.utc)
    assert messages[0]["conversation_id"] == "imessage-mac-relay"
    assert messages[0]["is_from_me"] is False
    assert last == 6  # scanned past skipped rows too


def test_include_sent_files_under_partner_and_skips_group(tmp_path):
    db = str(tmp_path / "chat.db")
    make_chat_db(db)
    messages, _ = mac_relay.read_new_messages(db, 0, include_sent=True)
    assert [m["rowid"] for m in messages] == [1, 2, 6]
    sent = messages[1]
    assert sent["is_from_me"] is True
    assert sent["sender_identifier"] == "+15550108888"  # the 1:1 partner
    assert sent["conversation_id"] == "imessage-mac-relay-sent"


def test_state_resumes_after_last_rowid(tmp_path):
    db = str(tmp_path / "chat.db")
    make_chat_db(db)
    messages, _ = mac_relay.read_new_messages(db, 1, include_sent=True)
    assert [m["rowid"] for m in messages] == [2, 6]


def test_payload_shape_matches_shortcut_endpoint():
    payload = mac_relay.build_payload(
        {
            "sender_identifier": "+15550108888",
            "message_text": "hi",
            "sent_at": datetime(2026, 11, 17, 23, 42, tzinfo=timezone.utc),
            "conversation_id": "imessage-mac-relay",
        }
    )
    assert payload["sender_identifier"] == "+15550108888"
    assert payload["sent_at"] == "2026-11-17T23:42:00+00:00"
    assert payload["conversation_id"] == "imessage-mac-relay"


def test_post_sends_token_header_and_json():
    captured = {}

    class FakeResp:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=15):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode())
        return FakeResp()

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        ok, status = mac_relay.post_message(
            "https://overlap.example.com/",
            "tok-123",
            {
                "sender_identifier": "+15550108888",
                "message_text": "hi",
                "sent_at": datetime(2026, 11, 17, 23, 42, tzinfo=timezone.utc),
                "conversation_id": "imessage-mac-relay",
            },
        )
    assert ok is True and status == 201
    assert captured["url"] == "https://overlap.example.com/ingest/shortcut"
    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers_lower["x-overlap-token"] == "tok-123"
    assert captured["body"]["sender_identifier"] == "+15550108888"


def test_run_once_dry_run_advances_no_state(tmp_path):
    db = str(tmp_path / "chat.db")
    make_chat_db(db)
    state = str(tmp_path / "state.json")
    posted = mac_relay.run_once(
        "https://overlap.example.com", "tok", db, state, False, True
    )
    assert posted == 2
    assert not os.path.exists(state)


def test_run_once_posts_and_saves_state(tmp_path):
    db = str(tmp_path / "chat.db")
    make_chat_db(db)
    state = str(tmp_path / "state.json")
    with mock.patch.object(mac_relay, "post_message", return_value=(True, 201)) as post:
        posted = mac_relay.run_once(
            "https://overlap.example.com", "tok", db, state, True, False
        )
    assert posted == 3
    assert post.call_count == 3
    assert mac_relay.load_state(state) == 6
    # Second pass finds nothing new.
    with mock.patch.object(mac_relay, "post_message", return_value=(True, 201)) as post2:
        assert mac_relay.run_once(
            "https://overlap.example.com", "tok", db, state, True, False
        ) == 0
    assert post2.call_count == 0


def test_run_once_stops_on_failure_and_retries(tmp_path):
    db = str(tmp_path / "chat.db")
    make_chat_db(db)
    state = str(tmp_path / "state.json")
    calls = {"n": 0}

    def flaky(server, token, message, timeout=15):
        calls["n"] += 1
        return (False, "HTTP 500") if calls["n"] == 2 else (True, 201)

    with mock.patch.object(mac_relay, "post_message", side_effect=flaky):
        posted = mac_relay.run_once(
            "https://overlap.example.com", "tok", db, state, True, False
        )
    assert posted == 1  # only the first message got through
    assert mac_relay.load_state(state) == 1  # failed ROWID 2 retries next pass
