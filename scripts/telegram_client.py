"""OVERLAP Telegram user client — the automatic Telegram tier.

Telegram is the one Tier-1 channel the bot shape cannot give you: a
bot only ever sees what a user sends it. Telegram's own user-account
API (MTProto) is different — it is how sanctioned third-party Telegram
clients exist. Log in once with your own account and a client can read
your own cloud chats automatically, forever, the way the iPhone
Shortcuts automation does for iMessage.

This script is that client. It runs on your own machine, signed into
*your* Telegram account, and posts each new message in your own chats
to the OVERLAP backend's POST /ingest/telegram endpoint, with the same
token auth as the iPhone Shortcuts automation.

Setup (one time, ~10 minutes, $0):

    pip install telethon
    # Get an api_id / api_hash at https://my.telegram.org/apps
    # (log in with your Telegram account, create an app — free).
    python3 scripts/telegram_client.py --server https://YOUR-OVERLAP-SERVER \\
        --token PASTE_YOUR_INGEST_TOKEN \\
        --api-id YOUR_API_ID --api-hash YOUR_API_HASH

The first run asks for your phone number and the login code Telegram
sends you, then saves a session file (default
~/.overlap-telegram.session) so later runs need no code. Keep that
session file private — it is a login to your Telegram account, the
same as a password. It can be revoked at any time in Telegram >
Settings > Devices > Terminate Session.

Run it in the foreground to listen:

    python3 scripts/telegram_client.py --server ... --token ... \\
        --api-id ... --api-hash ...

Safety / privacy, by design:

- It only ever reads the account you log into. It cannot read anyone
  else's Telegram.
- Only text, the sender's phone number (when Telegram shows it) or
  Telegram user id, and the send time leave the machine. Attachments
  (photos, video) are never sent.
- Secret chats are locked to the phone that started them and are
  invisible to any connected client, including this one — by design,
  not a bug. Cloud chats (the default) are what it sees.
- The ingest token works like a password: keep it out of screenshots
  and shared files. It can be revoked by deleting it server-side.

Honest limits:

- Sent messages are filed under the *partner* (the person on the other
  end of the 1:1 chat), because /ingest/telegram records one sender per
  message and the partner is the person being watched. They are posted
  with conversation_id "telegram-client-sent", separate from incoming
  ("telegram-client"), but the current flagging counts signals per
  sender hash, so sent messages count toward the partner's volume in
  the pattern check. Sent capture is therefore opt-in
  (--include-sent); incoming-only is the default, and matches what the
  bot shape sees.
- Group chats: incoming messages are filed under their real sender.
  Sent messages in group chats are skipped — there is no single
  partner to file them under.
- Text only. Messages whose text is empty (e.g. photo-only) are
  skipped, same as the bot route.
- This is a long-running client, not a webhook: the machine it runs
  on stays on while it listens. Stop it with Ctrl-C; the session file
  means the next run resumes without a new login code.
- A sender whose phone number Telegram hides is filed by Telegram
  user id ("telegram:<id>") instead. That id is the same in every
  owner's chats, so matching still works — but a person registered
  by phone in one place and by Telegram id in another will not match
  until both sides use the same identifier. Registering the person
  from their Telegram contact card (which carries the phone number)
  keeps the two in sync.
"""

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

CONVERSATION_INCOMING = "telegram-client"
CONVERSATION_SENT = "telegram-client-sent"

DEFAULT_SESSION_PATH = os.path.expanduser("~/.overlap-telegram.session")


def resolve_sender_identifier(sender):
    """The identifier OVERLAP files a message under.

    `sender` is a dict with optional "phone" and "id" keys (Telethon
    gives both on a User object). Prefer the phone number — it is the
    identifier the contact-card route already uses, so a person
    registered from a contact card and a person seen by this client
    match. Fall back to the Telegram user id when the phone is hidden.
    Returns None when neither is available.
    """
    phone = (sender or {}).get("phone") or ""
    if str(phone).strip():
        return str(phone).strip()
    user_id = (sender or {}).get("id")
    if user_id is not None:
        return f"telegram:{user_id}"
    return None


def build_payload(message):
    """The JSON body for one message at /ingest/telegram.

    `message` is a dict with: sender (dict, see resolve_sender_identifier),
    text, sent_at (aware datetime), is_from_me, is_group, partner
    (dict, the 1:1 chat partner — only used for sent messages).
    Returns None when the message should not be filed, with the reason
    in the second element of the tuple.
    """
    text = (message.get("text") or "").strip()
    if not text:
        return None, "empty message (photo-only, etc.) — skipped"
    if message.get("is_from_me"):
        if not message.get("include_sent"):
            return None, "sent message — skipped (pass --include-sent to file it)"
        if message.get("is_group"):
            return None, "sent message in a group chat — no single partner"
        partner = message.get("partner") or {}
        sender_identifier = resolve_sender_identifier(partner)
        conversation_id = CONVERSATION_SENT
    else:
        sender_identifier = resolve_sender_identifier(message.get("sender") or {})
        conversation_id = CONVERSATION_INCOMING
    if not sender_identifier:
        return None, "sender has neither a phone number nor a Telegram id"
    sent_at = message.get("sent_at") or datetime.now(timezone.utc)
    return (
        {
            "sender_identifier": sender_identifier,
            "message_text": text,
            "sent_at": sent_at.isoformat(),
            "conversation_id": conversation_id,
        },
        None,
    )


def post_message(server, token, payload, timeout=15):
    """Post one message; returns (ok, status_or_error)."""
    url = server.rstrip("/") + "/ingest/telegram"
    data = json.dumps(payload).encode("utf-8")
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


def handle_message(server, token, message, dry_run=False):
    """File one captured message. Returns True when it was posted (or
    would have been, in dry-run mode)."""
    payload, reason = build_payload(message)
    if payload is None:
        print(f"Skipped: {reason}.")
        return False
    if dry_run:
        print(
            f"[dry-run] {payload['conversation_id']} "
            f"{payload['sender_identifier']}: "
            f"{payload['message_text'][:60]!r}"
        )
        return True
    ok, status = post_message(server, token, payload)
    if not ok:
        print(f"Failed to post ({status}); message not filed.", file=sys.stderr)
        return False
    return True


async def run_client(server, token, api_id, api_hash, session_path,
                     include_sent, dry_run):
    """Listen for new messages on the user's own account (Telethon)."""
    try:
        from telethon import TelegramClient, events
    except ImportError:
        print(
            "Telethon is not installed. Install it first:\n"
            "    pip install telethon\n"
            "Then re-run this script.",
            file=sys.stderr,
        )
        return 1

    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()
    me = await client.get_me()
    print(f"Telegram client listening as {me.first_name or me.username or me.id} "
          f"(incoming only{' + sent' if include_sent else ''}). "
          "Stop with Ctrl-C.")

    @client.on(events.NewMessage)
    async def on_new_message(event):
        sender = await event.get_sender()
        sender_dict = {
            "phone": getattr(sender, "phone", None),
            "id": getattr(sender, "id", None),
        }
        partner_dict = None
        if event.is_private:
            partner = await event.get_chat()
            partner_dict = {
                "phone": getattr(partner, "phone", None),
                "id": getattr(partner, "id", None),
            }
        message = {
            "sender": sender_dict,
            "text": event.raw_text or "",
            "sent_at": event.date or datetime.now(timezone.utc),
            "is_from_me": bool(event.out),
            "is_group": not event.is_private,
            "partner": partner_dict,
            "include_sent": include_sent,
        }
        handle_message(server, token, message, dry_run=dry_run)

    await client.run_until_disconnected()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Listen to your own Telegram chats and file them "
        "with OVERLAP (automatic capture, Telethon user client)."
    )
    parser.add_argument("--server", required=True,
                        help="OVERLAP base URL, e.g. https://overlap.example.com")
    parser.add_argument("--token", default=os.environ.get("OVERLAP_INGEST_TOKEN", ""),
                        help="Ingest token (or set OVERLAP_INGEST_TOKEN). "
                        "Same token as the iPhone Shortcuts automation.")
    parser.add_argument("--api-id", type=int, default=int(os.environ.get("TELEGRAM_API_ID", "0") or 0),
                        help="Telegram api_id from https://my.telegram.org/apps "
                        "(or set TELEGRAM_API_ID)")
    parser.add_argument("--api-hash", default=os.environ.get("TELEGRAM_API_HASH", ""),
                        help="Telegram api_hash from https://my.telegram.org/apps "
                        "(or set TELEGRAM_API_HASH)")
    parser.add_argument("--session", default=DEFAULT_SESSION_PATH,
                        help="Telethon session file (default: ~/.overlap-telegram.session)")
    parser.add_argument("--include-sent", action="store_true",
                        help="Also file messages you sent (filed under the 1:1 partner; see module docstring)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be posted; post nothing")
    args = parser.parse_args(argv)

    if not args.token:
        parser.error("--token is required (or set OVERLAP_INGEST_TOKEN)")
    if not args.api_id or not args.api_hash:
        parser.error("--api-id and --api-hash are required "
                     "(get them at https://my.telegram.org/apps, "
                     "or set TELEGRAM_API_ID / TELEGRAM_API_HASH)")
    return asyncio.run(
        run_client(args.server, args.token, args.api_id, args.api_hash,
                   args.session, args.include_sent, args.dry_run)
    )


if __name__ == "__main__":
    raise SystemExit(main())
