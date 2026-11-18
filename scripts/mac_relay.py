"""OVERLAP Mac relay — iMessage capture from the user's own Mac.

Apple offers no webhook or API for personal iMessage, so messages can
only leave iMessage through a device signed into the account. This
script is the Mac half of that: it reads the Messages database on the
user's own Mac (~/Library/Messages/chat.db) and posts new messages to
the OVERLAP backend's existing /ingest/shortcut endpoint, with the same
token auth as the iPhone Shortcuts automation.

Why this exists: the Shortcuts automation only catches *incoming*
messages. The Mac database holds both directions, with the real send
time of each message — so the late-night pattern reading in flagging
gets true timestamps instead of ingest time.

Run on the Mac, from the project folder, e.g.:

    python3 scripts/mac_relay.py --server https://YOUR-OVERLAP-SERVER \\
        --token PASTE_YOUR_INGEST_TOKEN --once

    python3 scripts/mac_relay.py --server https://YOUR-OVERLAP-SERVER \\
        --token PASTE_YOUR_INGEST_TOKEN --daemon --interval 300

Safety / privacy, by design:

- The live database is never opened. A copy is made to a temp file and
  the copy is read read-only, so the relay can never corrupt Messages
  or fight the Messages app for a lock.
- Only text, the sender's phone/email, and the send time leave the
  Mac. Attachments (photos, video) are never sent.
- The ingest token works like a password: keep it out of screenshots
  and shared files. It can be revoked by deleting it server-side.
- macOS requires Full Disk Access for the terminal/python running this
  script to read ~/Library/Messages/chat.db. Without it the copy fails
  and the script says so plainly instead of silently sending nothing.
- The relay only ever reads the Mac it runs on, for the account signed
  into Messages there. It cannot read anyone else's iMessage.

Honest limits:

- Sent messages are filed under the *partner* (the person on the other
  end of the 1:1 chat), because /ingest/shortcut records one sender per
  message and the partner is the person being watched. They are posted
  with conversation_id "imessage-mac-relay-sent", separate from
  incoming ("imessage-mac-relay"), but the current flagging counts
  signals per sender hash, so sent messages count toward the partner's
  volume in the pattern check. Sent capture is therefore opt-in
  (--include-sent); incoming-only is the default, and matches what the
  Shortcuts automation sees.
- Group chats: incoming messages are filed under their real sender.
  Sent messages in group chats are skipped — there is no single
  partner to file them under.
- Text only. Messages whose text is empty (e.g. photo-only) are
  skipped, same as the Shortcuts route.
- The relay is a poller, not a push: with --daemon it checks every
  --interval seconds (default 300). A message is at most one interval
  late, never lost — state is the highest message ROWID already
  posted, so a restart resumes where it left off.
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

CONVERSATION_INCOMING = "imessage-mac-relay"
CONVERSATION_SENT = "imessage-mac-relay-sent"

DEFAULT_STATE_PATH = os.path.expanduser("~/.overlap-mac-relay-state.json")
DEFAULT_DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")


def apple_to_datetime(value):
    """Convert an Apple-epoch timestamp to an aware datetime.

    Messages stores `date` in nanoseconds since 2001-01-01. Very old
    rows (and some test fixtures) store seconds instead; the magnitude
    tells them apart.
    """
    if value is None:
        return datetime.now(timezone.utc)
    seconds = float(value)
    if abs(seconds) > 1e12:  # nanoseconds
        seconds = seconds / 1e9
    return APPLE_EPOCH + timedelta(seconds=seconds)


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def read_new_messages(db_path, last_rowid, include_sent=False):
    """Return (messages, new_last_rowid) for rows after `last_rowid`.

    Each message is a dict with sender_identifier, message_text,
    sent_at (aware datetime), is_from_me, and conversation_id.
    `db_path` must be a *copy* of chat.db, never the live file.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        msg_cols = _columns(conn, "message")
        has_item_type = "item_type" in msg_cols

        # Chat -> participant handles, for attributing sent messages.
        chat_handles = {}
        try:
            for chat_id, handle_id in conn.execute(
                "SELECT chj.chat_id, h.id FROM chat_handle_join chj "
                "JOIN handle h ON h.ROWID = chj.handle_id"
            ):
                chat_handles.setdefault(chat_id, []).append(handle_id)
        except sqlite3.Error:
            chat_handles = {}

        message_chat = {}
        try:
            for chat_id, message_id in conn.execute(
                "SELECT chat_id, message_id FROM chat_message_join"
            ):
                message_chat[message_id] = chat_id
        except sqlite3.Error:
            message_chat = {}

        item_filter = "AND (m.item_type = 0 OR m.item_type IS NULL)" if has_item_type else ""
        rows = conn.execute(
            f"""SELECT m.ROWID, m.guid, m.text, m.handle_id, m.is_from_me,
                       m.date, h.id
                FROM message m
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                WHERE m.ROWID > ? AND m.text IS NOT NULL
                  AND TRIM(m.text) != '' {item_filter}
                ORDER BY m.ROWID ASC""",
            (last_rowid,),
        ).fetchall()
    finally:
        conn.close()

    messages = []
    new_last = last_rowid
    for rowid, _guid, text, _handle_rowid, is_from_me, date, handle_id in rows:
        new_last = max(new_last, rowid)
        if is_from_me:
            if not include_sent:
                continue
            partners = chat_handles.get(message_chat.get(rowid), [])
            if len(partners) != 1:
                continue  # group chat: no single partner to file under
            sender = partners[0]
            conversation = CONVERSATION_SENT
        else:
            if not handle_id:
                continue  # unknown sender; nothing to attribute it to
            sender = handle_id
            conversation = CONVERSATION_INCOMING
        messages.append(
            {
                "rowid": rowid,
                "sender_identifier": sender,
                "message_text": text,
                "sent_at": apple_to_datetime(date),
                "is_from_me": bool(is_from_me),
                "conversation_id": conversation,
            }
        )
    return messages, new_last


def build_payload(message):
    """The JSON body for one message at /ingest/shortcut."""
    return {
        "sender_identifier": message["sender_identifier"],
        "message_text": message["message_text"],
        "sent_at": message["sent_at"].isoformat(),
        "conversation_id": message["conversation_id"],
    }


def post_message(server, token, message, timeout=15):
    """Post one message; returns (ok, status_or_error)."""
    url = server.rstrip("/") + "/ingest/shortcut"
    data = json.dumps(build_payload(message)).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Overlap-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            return True, resp.status
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # network down, bad server, bad token host
        return False, str(exc)


def load_state(state_path):
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return int(data.get("last_rowid", 0))
    except (OSError, ValueError, TypeError):
        return 0


def save_state(state_path, last_rowid):
    tmp = f"{state_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(
            {"last_rowid": last_rowid, "last_run": datetime.now(timezone.utc).isoformat()},
            fh,
        )
    os.replace(tmp, state_path)


def copy_database(db_path):
    """Copy chat.db to a temp file and return its path.

    Raises OSError with a plain explanation when macOS blocks the read
    (usually missing Full Disk Access).
    """
    tmpdir = tempfile.mkdtemp(prefix="overlap-mac-relay-")
    copy_path = os.path.join(tmpdir, "chat.db")
    try:
        shutil.copyfile(db_path, copy_path)
    except OSError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise OSError(
            "Could not read ~/Library/Messages/chat.db. On macOS this "
            "usually means the terminal running this script needs Full "
            "Disk Access (System Settings > Privacy & Security > Full "
            f"Disk Access). Underlying error: {exc}"
        ) from exc
    return copy_path


def run_once(server, token, db_path, state_path, include_sent, dry_run):
    """One polling pass. Returns the number of messages posted."""
    last_rowid = load_state(state_path)
    copy_path = copy_database(db_path)
    try:
        messages, new_last = read_new_messages(copy_path, last_rowid, include_sent)
    finally:
        shutil.rmtree(os.path.dirname(copy_path), ignore_errors=True)

    posted = 0
    for message in messages:
        if dry_run:
            print(
                f"[dry-run] ROWID {message['rowid']} "
                f"{'sent' if message['is_from_me'] else 'received'} "
                f"from {message['sender_identifier']}: "
                f"{message['message_text'][:60]!r}"
            )
            posted += 1
            continue
        ok, status = post_message(server, token, message)
        if not ok:
            # Stop here; state is saved only up to the last success,
            # so the failed message (and everything after it) retries
            # on the next pass instead of being silently skipped.
            print(
                f"Failed to post ROWID {message['rowid']} ({status}); "
                "stopping this pass. State saved up to the last success.",
                file=sys.stderr,
            )
            break
        posted += 1
        last_rowid = message["rowid"]

    if dry_run:
        print(f"Dry run: {posted} message(s) would be posted. State not advanced.")
    else:
        # Advance past rows that were skipped (empty/group/sent when
        # not included) as well as posted ones, so they are not
        # re-read forever; failed posts keep last_rowid behind them.
        save_state(state_path, max(last_rowid, new_last if posted == len(messages) else last_rowid))
        print(f"Posted {posted} message(s); last ROWID now {load_state(state_path)}.")
    return posted


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Relay new iMessages from this Mac to OVERLAP."
    )
    parser.add_argument("--server", required=True, help="OVERLAP base URL, e.g. https://overlap.example.com")
    parser.add_argument(
        "--token",
        default=os.environ.get("OVERLAP_INGEST_TOKEN", ""),
        help="Ingest token (or set OVERLAP_INGEST_TOKEN). Same token as the iPhone Shortcuts automation.",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to chat.db (default: ~/Library/Messages/chat.db)")
    parser.add_argument("--state", default=DEFAULT_STATE_PATH, help="State file holding the last posted ROWID")
    parser.add_argument("--include-sent", action="store_true", help="Also relay messages you sent (filed under the 1:1 partner; see module docstring)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be posted; post nothing, advance no state")
    parser.add_argument("--once", action="store_true", help="Do a single pass and exit")
    parser.add_argument("--daemon", action="store_true", help="Poll forever until stopped")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between passes in --daemon mode (default 300)")
    args = parser.parse_args(argv)

    if not args.token and not args.dry_run:
        parser.error("--token is required (or set OVERLAP_INGEST_TOKEN)")
    if args.daemon and args.once:
        parser.error("Use --daemon or --once, not both")
    if not args.daemon and not args.once:
        args.once = True  # single pass is the safe default

    if args.daemon:
        print(f"Mac relay running: polling every {args.interval}s. Stop with Ctrl-C.")
        try:
            while True:
                run_once(args.server, args.token, args.db, args.state, args.include_sent, args.dry_run)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("Mac relay stopped.")
        return 0

    run_once(args.server, args.token, args.db, args.state, args.include_sent, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
