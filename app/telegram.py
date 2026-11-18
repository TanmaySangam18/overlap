"""Telegram transport for the OVERLAP agent — two shapes, one brain.

1. **Bot API (this file).** OVERLAP as a Telegram bot, made in minutes
   with @BotFather — no business account, no business verification, no
   phone number, no 24-hour token. Telegram sends each message the user
   sends *into* the bot's chat here via webhook, exactly like the
   WhatsApp bot: text, forwarded messages, contact cards, and .txt/.zip
   chat-export documents. Buttons arrive as callback queries and are
   answered with inline keyboards.

   Configuration (environment):
   - TELEGRAM_BOT_TOKEN       BotFather token (enables live sending and
                              export-file downloads)
   - TELEGRAM_WEBHOOK_SECRET  Optional secret Telegram sends back in the
                              X-Telegram-Bot-Api-Secret-Token header.
                              When set, a POST without the right secret
                              is rejected with 403. When unset (dev/test),
                              deliveries are allowed — set it in prod.

   Without the bot token the transport runs in dry-run mode: outbound
   messages are recorded in DRY_RUN_LOG instead of sent, so the whole
   bot can be exercised in tests and simulations.

2. **MTProto user client (scripts/telegram_client.py).** That is the
   other shape — the automatic one. A user logs in once with their own
   Telegram account (Telethon, Telegram's sanctioned third-party-client
   API) and the client reads their own cloud chats automatically,
   forever, posting to POST /ingest/telegram. Secret chats are locked
   to the phone that started them and are invisible to it, by design.

Why the bot alone is not "automatic like iMessage": a bot can only
see what a user sends it. Telegram forwards *do* preserve the origin
in the payload (forward_origin: a user id when visible, a display
name when the sender hid it) — unlike WhatsApp, which strips the
sender — but OVERLAP still attributes a forward the same way it does
on WhatsApp (one tap on the name), because the match key is the
person's phone / Telegram id as registered from their contact card,
not the raw forward payload. The client is what makes Telegram
automatic; the bot is what makes it testable in five minutes with no
account of the user's own at stake.
"""

import json
import os
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from . import agent, ratelimit, store

router = APIRouter()
API_BASE = "https://api.telegram.org"
DRY_RUN_LOG = []


def _bot_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _webhook_secret() -> str:
    return os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")


def live() -> bool:
    return bool(_bot_token())


def _api_post(method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}/bot{_bot_token()}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_document_bytes(file_id: str) -> bytes:
    """Download an inbound document (chat exports arrive this way)."""
    meta = _api_post("getFile", {"file_id": file_id})
    file_path = (meta.get("result") or {}).get("file_path", "")
    if not file_path:
        raise ValueError("Telegram did not return a file path.")
    req = urllib.request.Request(f"{API_BASE}/file/bot{_bot_token()}/{file_path}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def send_message(to_chat_id, text: str, buttons=None) -> dict:
    """Send one bot message. Dry-run (logged) unless live-configured."""
    buttons = [b for b in (buttons or []) if b.get("id") and b.get("title")]
    if not live():
        record = {
            "to": str(to_chat_id),
            "text": text,
            "buttons": buttons,
            "dry_run": True,
        }
        DRY_RUN_LOG.append(record)
        return record
    payload = {"chat_id": to_chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [
                [
                    {
                        "text": b["title"][:64],
                        "callback_data": b["id"][:64],
                    }
                    for b in buttons[:3]
                ]
            ]
        }
    result = _api_post("sendMessage", payload)
    return {"to": str(to_chat_id), "text": text, "buttons": buttons, "result": result}


def _iso_from_epoch(ts) -> str:
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)
        return dt.isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _display_name(user: dict) -> str | None:
    if not user:
        return None
    name = " ".join(
        part for part in (user.get("first_name"), user.get("last_name")) if part
    ).strip()
    return name or user.get("username") or None


def _event_from_message(msg: dict):
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    base = {
        "channel": "telegram",
        "address": str(chat.get("id", sender.get("id", ""))),
        "display_name": _display_name(sender),
        "message_id": str(msg.get("message_id", "")),
        "sent_at": _iso_from_epoch(msg.get("date")),
        "text": "",
        "contact": None,
        "document_bytes": None,
        "button_id": None,
    }
    if "contact" in msg:
        contact = msg["contact"]
        base["kind"] = "contact"
        base["contact"] = {
            "name": " ".join(
                p
                for p in (contact.get("first_name"), contact.get("last_name"))
                if p
            ).strip(),
            "phone": contact.get("phone_number", ""),
            "telegram_id": contact.get("user_id"),
        }
        return base
    if "document" in msg:
        doc = msg["document"] or {}
        filename = (doc.get("file_name") or "").lower()
        mime = doc.get("mime_type") or ""
        if not (
            filename.endswith((".txt", ".zip"))
            or mime in ("text/plain", "application/zip")
        ):
            base["kind"] = "text"
            base["text"] = ""
            base["unsupported_document"] = True
            return base
        base["kind"] = "document"
        base["file_id"] = doc.get("file_id")
        return base
    if "text" in msg:
        base["text"] = msg.get("text") or ""
        # Telegram preserves the forward origin (a user id when the
        # sender allows it, a display name when they hid it); WhatsApp
        # strips it. Attribution still goes through the agent's tap
        # flow — see the module docstring — but the origin is kept on
        # the event so a future auto-attribution step has it.
        origin = msg.get("forward_origin") or {}
        if msg.get("forward_from") or origin:
            base["kind"] = "forwarded"
            origin_user = origin.get("sender_user") or {}
            if origin_user.get("id"):
                base["forwarded_from_telegram_id"] = origin_user["id"]
            elif origin.get("sender_user_name"):
                base["forwarded_from_name"] = origin["sender_user_name"]
        else:
            base["kind"] = "text"
        return base
    return None  # photos, stickers, voice, etc. are not agent input


def _event_from_update(update: dict):
    if "callback_query" in update:
        cb = update["callback_query"] or {}
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        sender = cb.get("from") or {}
        return {
            "channel": "telegram",
            "address": str(chat.get("id", sender.get("id", ""))),
            "display_name": _display_name(sender),
            "message_id": str(cb.get("id", "")),
            "sent_at": _iso_from_epoch(msg.get("date")),
            "kind": "button",
            "text": (cb.get("data") or ""),
            "contact": None,
            "document_bytes": None,
            "button_id": cb.get("data") or "",
        }
    if "message" in update:
        return _event_from_message(update["message"])
    return None


def _verify_secret(request: Request) -> None:
    secret = _webhook_secret()
    if not secret:
        return
    provided = request.headers.get("x-telegram-bot-api-secret-token", "")
    if provided != secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret.")


@router.post("/webhooks/telegram")
async def receive(request: Request):
    _verify_secret(request)
    ratelimit.enforce_ip(request)
    try:
        payload = json.loads(await request.body())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    ev = _event_from_update(payload)
    if ev is None:
        return {"status": "ok"}
    if ev.get("address"):
        ratelimit.enforce_sender(f"telegram:{ev['address']}")
    if ev["kind"] == "document":
        try:
            ev["document_bytes"] = fetch_document_bytes(ev.get("file_id", ""))
        except Exception:
            send_message(
                ev["address"],
                "I couldn't download that file — if the live Telegram "
                "connection isn't configured yet, exports can't reach me. "
                "Try again once OVERLAP is live.",
            )
            return {"status": "ok"}
    if ev.get("unsupported_document"):
        send_message(
            ev["address"],
            "I can read chat exports as .txt or .zip files. Telegram's "
            "own export is a different format — forward messages "
            "instead, or use the Telegram client for automatic capture.",
        )
        return {"status": "ok"}
    owner_id = store.owner_id_for_address("telegram", ev["address"])
    for reply in agent.handle_event(ev):
        try:
            send_message(ev["address"], reply["text"], reply.get("buttons"))
        except Exception:
            store.enqueue_outbox(
                owner_id, "telegram", ev["address"],
                reply["text"], reply.get("buttons"),
            )
    # Deliver anything the agent owes other users (match/flag notices).
    # Peek + mark-after-send, same as WhatsApp: a failed send stays
    # queued instead of being marked sent and lost.
    for row in store.peek_outbox(channel="telegram"):
        try:
            send_message(row["address"], row["text"], row["buttons"])
        except Exception:
            continue
        store.mark_outbox_sent([row["id"]])
    return {"status": "ok"}
