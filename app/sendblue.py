"""Sendblue transport for the OVERLAP agent — the iMessage line.

This is the architecture the iMessage-native agents (Series and peers)
actually use: an iMessage API provider runs real Apple devices and
gives the agent its OWN phone number. Users text that number in
Messages (blue bubbles), the provider POSTs each inbound message to
our webhook, and replies go out over its REST API. No Mac of ours, no
Meta account, no access token that dies in 24 hours.

Configuration (environment):
- SENDBLUE_API_KEY_ID    API key id    (enables live sending)
- SENDBLUE_API_SECRET_KEY API secret
- SENDBLUE_FROM_NUMBER   the OVERLAP line's number, E.164
- SENDBLUE_WEBHOOK_SECRET optional; when set, webhook POSTs must carry
                          the same value in the sb-signing-secret
                          header or they are rejected with 403.

Without keys the transport runs in dry-run mode: outbound messages are
recorded in DRY_RUN_LOG instead of sent, so the whole bot can be
exercised in tests and simulations.

Channel limits, by design:
- iMessage has no reply buttons, so options render as text — the
  engine already understands typed replies (YES / DONE / a name /
  CONFIRM / LOG IT) on every channel.
- iMessage has no "forwarded" flag, so everything a user sends is
  conversation until it looks like a pasted chat — and pastes are
  staged and filed only on the user's "log it", never silently.
- A shared contact card arrives as a .vcf media attachment; it is
  parsed (name + first phone) so onboarding works like WhatsApp's.
"""

import hashlib
import hmac
import json
import os
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from . import agent, ratelimit, store
from .sms import _render
from .whatsapp import _chunks

router = APIRouter()
API_BASE = "https://api.sendblue.co"
DRY_RUN_LOG = []


def _key_id() -> str:
    return os.environ.get("SENDBLUE_API_KEY_ID", "")


def _secret_key() -> str:
    return os.environ.get("SENDBLUE_API_SECRET_KEY", "")


def _from_number() -> str:
    return os.environ.get("SENDBLUE_FROM_NUMBER", "")


def _webhook_secret() -> str:
    return os.environ.get("SENDBLUE_WEBHOOK_SECRET", "")


def live() -> bool:
    return bool(_key_id() and _secret_key() and _from_number())


def _api_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "sb-api-key-id": _key_id(),
            "sb-api-secret-key": _secret_key(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_message(to_number: str, text: str, buttons=None) -> dict:
    """Send one bot message. Dry-run (logged) unless live-configured.

    Buttons render as text options — iMessage has no tappable reply
    buttons, and the engine accepts the typed equivalents.
    """
    rendered = _render({"text": text, "buttons": buttons or []})
    if not live():
        record = {"to": to_number, "text": rendered, "dry_run": True}
        DRY_RUN_LOG.append(record)
        return record
    result = None
    for chunk in _chunks(rendered):
        result = _api_post(
            "/api/send-message",
            {"number": to_number, "from_number": _from_number(), "content": chunk},
        )
    return {"to": to_number, "text": rendered, "result": result}


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_vcard(text: str):
    """(name, phone) from a vCard body, or None. First TEL wins."""
    name, phone = "", ""
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("FN:") and not name:
            name = line[3:].strip()
        elif line.startswith("TEL") and not phone:
            phone = line.split(":", 1)[-1].strip()
    if phone:
        return {"name": name, "phone": phone, "wa_id": ""}
    return None


def _iso(sent) -> str:
    if isinstance(sent, str) and sent:
        try:
            dt = datetime.fromisoformat(sent.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt.isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _event_from_payload(payload: dict):
    """One Sendblue receive-webhook payload -> one agent event (or None)."""
    if payload.get("is_outbound"):
        return None  # our own sends echoing back are not agent input
    if payload.get("group_id"):
        return None  # OVERLAP is a 1:1 conversation; groups are not input
    address = payload.get("from_number") or ""
    content = payload.get("content") or ""
    media_url = payload.get("media_url") or ""
    message_id = payload.get("message_handle") or hashlib.sha256(
        f"{address}|{payload.get('date_sent')}|{content}|{media_url}".encode()
    ).hexdigest()[:32]
    base = {
        "channel": "sendblue",
        "address": address,
        "display_name": None,
        "message_id": message_id,
        "sent_at": _iso(payload.get("date_sent")),
        "text": content,
        "contact": None,
        "document_bytes": None,
        "button_id": None,
        "kind": "text",
    }
    if media_url and media_url.split("?")[0].lower().endswith(".vcf"):
        try:
            card = _parse_vcard(_fetch_text(media_url))
        except Exception:
            card = None
        if card:
            base["kind"] = "contact"
            base["contact"] = card
            base["text"] = ""
            return base
        base["unsupported_message"] = "contact_card"
        return base
    if media_url and not content:
        base["unsupported_message"] = "media"
        return base
    if not content and not media_url:
        return None
    return base


def _verify_webhook(request: Request) -> None:
    """Reject forged webhooks when a signing secret is configured.

    Sendblue sends the shared secret itself in the sb-signing-secret
    header. With no secret configured (dev / $0 test rig) deliveries
    are allowed — set SENDBLUE_WEBHOOK_SECRET in production.
    """
    secret = _webhook_secret()
    if not secret:
        return
    provided = request.headers.get("sb-signing-secret", "")
    if not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret.")


@router.post("/webhooks/sendblue")
async def receive(request: Request):
    _verify_webhook(request)
    ratelimit.enforce_ip(request)
    try:
        payload = json.loads(await request.body())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    ev = _event_from_payload(payload)
    if ev is None:
        return {"status": "ok"}
    if ev.get("address"):
        ratelimit.enforce_sender(f"sendblue:{ev['address']}")
    if ev.get("unsupported_message") == "media":
        send_message(
            ev["address"],
            "I got the attachment, but I work with text — paste the "
            "messages, or send the person's contact card and forwards.",
        )
        return {"status": "ok"}
    if ev.get("unsupported_message") == "contact_card":
        send_message(
            ev["address"],
            "I couldn't read that contact card — type the person's "
            "number instead and I'll add it.",
        )
        return {"status": "ok"}
    owner_id = store.owner_id_for_address("sendblue", ev["address"])
    for reply in agent.handle_event(ev):
        try:
            send_message(ev["address"], reply["text"], reply.get("buttons"))
        except Exception:
            # The reply matters (it may be a match/flag notice): queue
            # it for the next delivery instead of losing it.
            store.enqueue_outbox(
                owner_id, "sendblue", ev["address"],
                reply["text"], reply.get("buttons"),
            )
    # Deliver anything the agent owes other users (match/flag notices).
    # Peek + mark-after-send: a failed send stays queued for next time.
    for row in store.peek_outbox(channel="sendblue"):
        try:
            send_message(row["address"], row["text"], row["buttons"])
        except Exception:
            continue
        store.mark_outbox_sent([row["id"]])
    return {"status": "ok"}
