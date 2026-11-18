"""WhatsApp transport for the OVERLAP agent (official Cloud API).

OVERLAP is a contact in the user's WhatsApp: a WhatsApp Business number
whose messages land here via webhook. Meta delivers only what users send
INTO this conversation — forwarded messages arrive unattributed (Meta
strips the original sender by design), contact cards arrive structured
with the number, and chat exports arrive as document messages.

Configuration (environment):
- WHATSAPP_VERIFY_TOKEN    webhook verification token (you choose it)
- WHATSAPP_ACCESS_TOKEN    Cloud API access token (enables live sending
                           and export-file downloads)
- WHATSAPP_PHONE_NUMBER_ID the OVERLAP number's phone_number_id
- WHATSAPP_APP_SECRET      Meta app secret. When set, every webhook POST
                           must carry a valid X-Hub-Signature-256 header
                           (HMAC-SHA256 of the raw body) or it is rejected
                           with 403. When unset (dev/test), signatures
                           are not checked.

Without the access token the transport runs in dry-run mode: outbound
messages are recorded in DRY_RUN_LOG instead of sent, so the whole bot
can be exercised in tests and simulations.
"""

import hashlib
import hmac
import json
import os
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from . import agent, ratelimit, store

router = APIRouter()
GRAPH_VERSION = "v21.0"
DRY_RUN_LOG = []


def _verify_token() -> str:
    return os.environ.get("WHATSAPP_VERIFY_TOKEN", "overlap-dev-verify-token")


def _access_token() -> str:
    return os.environ.get("WHATSAPP_ACCESS_TOKEN", "")


def _phone_number_id() -> str:
    return os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")


def live() -> bool:
    return bool(_access_token() and _phone_number_id())


def _graph_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_media_bytes(media_id: str) -> bytes:
    """Download an inbound media file (chat exports arrive this way)."""
    req = urllib.request.Request(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{media_id}",
        headers={"Authorization": f"Bearer {_access_token()}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        meta = json.loads(resp.read().decode("utf-8"))
    req = urllib.request.Request(
        meta["url"], headers={"Authorization": f"Bearer {_access_token()}"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _chunks(text: str, limit: int = 4096):
    """Split a long plain-text reply into WhatsApp-sized pieces.

    WhatsApp rejects text bodies over 4096 chars; a long status or
    evidence report must arrive as a few messages, not an API error.
    Splits on line boundaries where possible.
    """
    if len(text) <= limit:
        return [text]
    out, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                out.append(current)
                current = ""
            out.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            out.append(current)
            current = line
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def send_message(to_wa_id: str, text: str, buttons=None) -> dict:
    """Send one bot message. Dry-run (logged) unless live-configured."""
    buttons = [b for b in (buttons or []) if b.get("id") and b.get("title")]
    if not live():
        record = {"to": to_wa_id, "text": text, "buttons": buttons, "dry_run": True}
        DRY_RUN_LOG.append(record)
        return record
    payload = {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "interactive" if buttons else "text",
    }
    if buttons:
        payload["interactive"] = {
            "type": "button",
            "body": {"text": text[:1024]},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                    for b in buttons[:3]
                ]
            },
        }
        result = _graph_post(f"{_phone_number_id()}/messages", payload)
        return {"to": to_wa_id, "text": text, "buttons": buttons, "result": result}
    result = None
    for chunk in _chunks(text):
        payload["text"] = {"body": chunk}
        result = _graph_post(f"{_phone_number_id()}/messages", payload)
    return {"to": to_wa_id, "text": text, "buttons": buttons, "result": result}


def _in_window(owner_id: str) -> bool:
    """True when the user's last inbound message is < 24h old."""
    ts = store.last_inbound_at(owner_id)
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(ts)
    except ValueError:
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - last).total_seconds() < 24 * 3600


def _iso_from_epoch(ts) -> str:
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)
        return dt.isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _events_from_payload(payload: dict):
    events = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            profiles = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in value.get("contacts", [])
            }
            for msg in value.get("messages", []):
                events.extend(_events_from_message(msg, profiles))
    return events


def _events_from_message(msg: dict, profiles: dict):
    """One payload message -> one or more agent events.

    A contacts message can carry several cards at once (onboarding
    asks for up to 3 people) — each card becomes its own event with a
    suffixed message id, so webhook dedupe doesn't swallow cards 2+.
    """
    if msg.get("type") == "contacts" and len(msg.get("contacts") or []) > 1:
        out = []
        for idx, card in enumerate(msg["contacts"]):
            single = dict(msg)
            single["contacts"] = [card]
            ev = _event_from_message(single, profiles)
            if ev:
                ev["message_id"] = f"{msg.get('id')}#card{idx}"
                out.append(ev)
        return out
    ev = _event_from_message(msg, profiles)
    return [ev] if ev else []


def _event_from_message(msg: dict, profiles: dict):
    base = {
        "channel": "whatsapp",
        "address": msg.get("from", ""),
        "display_name": profiles.get(msg.get("from", "")),
        "message_id": msg.get("id"),
        "sent_at": _iso_from_epoch(msg.get("timestamp")),
        "text": "",
        "contact": None,
        "document_bytes": None,
        "button_id": None,
    }
    mtype = msg.get("type")
    if mtype == "text":
        base["text"] = (msg.get("text") or {}).get("body", "")
        forwarded = (msg.get("context") or {}).get("forwarded") or (
            msg.get("context") or {}
        ).get("frequently_forwarded")
        base["kind"] = "forwarded" if forwarded else "text"
        return base
    if mtype == "contacts":
        cards = msg.get("contacts") or []
        if not cards:
            return None
        card = cards[0]
        phones = card.get("phones") or [{}]
        base["kind"] = "contact"
        base["contact"] = {
            "name": (card.get("name") or {}).get("formatted_name", ""),
            "phone": phones[0].get("phone", ""),
            "wa_id": phones[0].get("wa_id", ""),
        }
        return base
    if mtype == "document":
        doc = msg.get("document") or {}
        filename = (doc.get("filename") or "").lower()
        mime = doc.get("mime_type") or ""
        if not (filename.endswith((".txt", ".zip")) or mime in ("text/plain", "application/zip")):
            base["kind"] = "text"
            base["text"] = ""
            base["unsupported_document"] = True
            return base
        base["kind"] = "document"
        base["media_id"] = doc.get("id")
        return base
    if mtype == "interactive":
        inter = msg.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        base["kind"] = "button"
        base["button_id"] = reply.get("id", "")
        base["text"] = reply.get("title", "")
        return base
    if mtype == "button":
        base["kind"] = "button"
        base["button_id"] = (msg.get("button") or {}).get("payload", "")
        base["text"] = (msg.get("button") or {}).get("text", "")
        return base
    if mtype in ("image", "audio", "video", "sticker", "location"):
        # Not agent input — but silence feels broken. The webhook
        # answers with one line of guidance instead of nothing.
        base["kind"] = "text"
        base["unsupported_message"] = mtype
        return base
    return None  # reactions, statuses, etc. are not agent input


@router.get("/webhooks/whatsapp")
def verify(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
):
    from fastapi.responses import PlainTextResponse

    if hub_mode == "subscribe" and hub_verify_token == _verify_token():
        return PlainTextResponse(hub_challenge)
    return PlainTextResponse("verification failed", status_code=403)


def _app_secret() -> str:
    return os.environ.get("WHATSAPP_APP_SECRET", "")


def _verify_signature(request: Request, raw_body: bytes) -> None:
    """Reject forged webhooks when an app secret is configured.

    Meta signs every delivery: X-Hub-Signature-256 is "sha256=" + the
    HMAC-SHA256 of the RAW request body under the app secret. The check
    must run on the raw bytes, before any JSON parsing. With no secret
    configured (local dev / the $0 test rig) there is nothing to verify
    against, so deliveries are allowed — set WHATSAPP_APP_SECRET in prod.
    """
    secret = _app_secret()
    if not secret:
        return
    expected = (
        "sha256="
        + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    )
    provided = request.headers.get("x-hub-signature-256", "")
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid webhook signature.")


@router.post("/webhooks/whatsapp")
async def receive(request: Request):
    raw = await request.body()
    _verify_signature(request, raw)
    ratelimit.enforce_ip(request)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    events = _events_from_payload(payload)
    # One sender flooding the bot must not take down the endpoint; a
    # batched payload is rejected whole (Meta retries, the flood keeps
    # tripping, legitimate bursts from one human stay far under budget).
    for sender in {ev["address"] for ev in events if ev.get("address")}:
        ratelimit.enforce_sender(f"whatsapp:{sender}")
    for ev in events:
        if ev["kind"] == "document":
            try:
                ev["document_bytes"] = fetch_media_bytes(ev.get("media_id", ""))
            except Exception:
                send_message(
                    ev["address"],
                    "I couldn't download that file — if the live WhatsApp "
                    "connection isn't configured yet, exports can't reach me. "
                    "Try again once OVERLAP is live.",
                )
                continue
        if ev.get("unsupported_document"):
            send_message(
                ev["address"],
                "I can read WhatsApp chat exports (.txt or .zip). Other files "
                "don't tell me anything.",
            )
            continue
        if ev.get("unsupported_message"):
            guidance = {
                "image": "I got the image, but I can't read screenshots "
                "inside WhatsApp — paste the text, forward the messages, "
                "or use the screenshot Shortcut from your Photos app.",
                "audio": "I can't listen to voice notes yet — forward me "
                "the messages as text and I'll log them.",
            }.get(
                ev["unsupported_message"],
                "I can work with text, forwards, contact cards and chat "
                "exports — that kind of message doesn't tell me anything.",
            )
            send_message(ev["address"], guidance)
            continue
        owner_id = store.owner_id_for_address("whatsapp", ev["address"])
        store.touch_inbound(owner_id)
        for reply in agent.handle_event(ev):
            try:
                send_message(ev["address"], reply["text"], reply.get("buttons"))
            except Exception:
                # The reply matters (it may be a match/flag notice):
                # queue it for the next delivery instead of losing it
                # to a transient send failure.
                store.enqueue_outbox(
                    owner_id, "whatsapp", ev["address"],
                    reply["text"], reply.get("buttons"),
                )
    # Deliver anything the agent owes other users (match/flag notices).
    # Peek + mark-after-send: a failed send stays queued for next time
    # instead of being marked sent and lost.
    #
    # WINDOW RULE (Meta policy): free-form messages may only be sent
    # within 24 hours of the recipient's last inbound message. Outside
    # the window Meta rejects the send (error 131047) — and attempting
    # it anyway is a policy violation. So out-of-window rows are HELD,
    # untouched, until that user messages again (which reopens their
    # window and drains their notice in the same request). Out-of-window
    # delivery would need an approved utility template — a later phase.
    for row in store.peek_outbox(channel="whatsapp"):
        if not _in_window(row["owner_id"]):
            continue
        try:
            send_message(row["address"], row["text"], row["buttons"])
        except Exception:
            continue
        store.mark_outbox_sent([row["id"]])
    return {"status": "ok"}
