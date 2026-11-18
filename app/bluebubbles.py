"""BlueBubbles transport for the OVERLAP agent — the in-house iMessage line.

No rented number, no provider, no Meta account. The free, open-source
BlueBubbles Server runs on the owner's own Mac, signed into an Apple
ID, and exposes iMessage over a REST API + webhooks:

- Inbound: the Mac POSTs every new message to /webhooks/bluebubbles.
- Outbound: replies are POSTed to the Mac's
  /api/v1/message/text endpoint, and Messages sends them as real
  iMessages (blue bubbles) from that Apple ID.

Run it under a dedicated Apple ID and the line is OVERLAP's own
number/address — the Series shape, except the "device farm" is one
Mac we own. Run it under a personal Apple ID and it is that person's
own Messages, automated — same code, different account.

Configuration (environment):
- BLUEBUBBLES_SERVER_URL  base URL the backend can reach the Mac at
                          (a tunnel URL — Cloudflare Tunnel / zrok /
                          ngrok — since the Mac sits behind a home
                          router). Enables live sending.
- BLUEBUBBLES_PASSWORD    the BlueBubbles server password.
- BLUEBUBBLES_WEBHOOK_SECRET optional; when set, webhook POSTs must
                          carry ?key=<secret> in the URL (BlueBubbles
                          webhooks cannot send custom headers) or
                          they are rejected with 403.

Without URL/password the transport runs in dry-run mode: outbound
messages are recorded in DRY_RUN_LOG instead of sent.

Channel notes:
- iMessage has no reply buttons, so options render as text — the
  engine already understands typed replies (YES / DONE / a name /
  CONFIRM / LOG IT) on every channel.
- iMessage has no "forwarded" flag, so typed text is conversation; a
  pasted chat is staged and filed only on the user's "log it".
- A shared contact card arrives as a .vcf attachment; it is fetched
  from the Mac and parsed (name + first phone), so onboarding works
  like WhatsApp's.
- The Mac's own sent messages (isFromMe) and group chats are ignored:
  OVERLAP is a 1:1 agent line.
"""

import hashlib
import hmac
import json
import os
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from . import agent, ratelimit, store
from .sendblue import _parse_vcard
from .sms import _render
from .whatsapp import _chunks

router = APIRouter()
DRY_RUN_LOG = []
CHAT_GUIDS = {}  # sender address -> chatGuid, learned from inbound events


def _server_url() -> str:
    return os.environ.get("BLUEBUBBLES_SERVER_URL", "").rstrip("/")


def _password() -> str:
    return os.environ.get("BLUEBUBBLES_PASSWORD", "")


def _webhook_secret() -> str:
    return os.environ.get("BLUEBUBBLES_WEBHOOK_SECRET", "")


def live() -> bool:
    return bool(_server_url() and _password())


def _api_url(path: str) -> str:
    return f"{_server_url()}{path}?password={urllib.parse.quote(_password())}"


def _api_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        _api_url(path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_attachment_bytes(attachment_guid: str) -> bytes:
    """Download an inbound attachment (contact cards arrive this way)."""
    url = _api_url(
        f"/api/v1/attachment/{urllib.parse.quote(attachment_guid, safe='')}/download"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


def _chat_guid_for(address: str, chat_guid: str = "") -> str:
    if chat_guid:
        CHAT_GUIDS[address] = chat_guid
        return chat_guid
    if address in CHAT_GUIDS:
        return CHAT_GUIDS[address]
    return f"iMessage;-;{address}"


def send_message(to_address: str, text: str, buttons=None, chat_guid: str = "") -> dict:
    """Send one bot message via the Mac. Dry-run unless live-configured."""
    rendered = _render({"text": text, "buttons": buttons or []})
    if not live():
        record = {"to": to_address, "text": rendered, "dry_run": True}
        DRY_RUN_LOG.append(record)
        return record
    guid = _chat_guid_for(to_address, chat_guid)
    result = None
    for chunk in _chunks(rendered):
        result = _api_post(
            "/api/v1/message/text",
            {
                "chatGuid": guid,
                "tempGuid": f"temp-{uuid.uuid4()}",
                "message": chunk,
            },
        )
    return {"to": to_address, "text": rendered, "result": result}


def _iso_from_bluebubbles(ts) -> str:
    """BlueBubbles dateCreated is milliseconds since the epoch."""
    try:
        seconds = int(ts) / 1000 if int(ts) > 10**11 else int(ts)
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
        return dt.isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _event_from_payload(payload: dict):
    """One BlueBubbles webhook payload -> one agent event (or None)."""
    if payload.get("type") != "new-message":
        return None
    data = payload.get("data") or {}
    if data.get("isFromMe"):
        return None  # the line's own sends are not agent input
    chats = data.get("chats") or []
    chat_guid = (chats[0] or {}).get("guid", "") if chats else ""
    if ";+;" in chat_guid:
        return None  # group chat — OVERLAP is a 1:1 conversation
    address = (data.get("handle") or {}).get("address", "")
    if not address:
        return None
    if chat_guid:
        CHAT_GUIDS[address] = chat_guid
    text = data.get("text") or ""
    base = {
        "channel": "bluebubbles",
        "address": address,
        "display_name": None,
        "message_id": data.get("guid")
        or hashlib.sha256(f"{address}|{data.get('dateCreated')}|{text}".encode()).hexdigest()[:32],
        "sent_at": _iso_from_bluebubbles(data.get("dateCreated")),
        "text": text,
        "contact": None,
        "document_bytes": None,
        "button_id": None,
        "kind": "text",
        "_chat_guid": chat_guid,
    }
    attachments = data.get("attachments") or []
    vcard = next(
        (
            a
            for a in attachments
            if (a.get("mimeType") in ("text/vcard", "text/x-vcard"))
            or (a.get("transferName") or "").lower().endswith(".vcf")
        ),
        None,
    )
    if vcard is not None:
        try:
            card = _parse_vcard(fetch_attachment_bytes(vcard.get("guid", "")).decode("utf-8", errors="replace"))
        except Exception:
            card = None
        if card:
            base["kind"] = "contact"
            base["contact"] = card
            base["text"] = ""
            return base
        base["unsupported_message"] = "contact_card"
        return base
    if attachments and not text:
        base["unsupported_message"] = "media"
        return base
    if not text and not attachments:
        return None
    return base


def _verify_webhook(request: Request) -> None:
    """Reject forged webhooks when a secret is configured.

    BlueBubbles webhooks cannot send custom headers, so the secret
    rides in the registered webhook URL as ?key=<secret>. With no
    secret configured (dev / $0 test rig) deliveries are allowed —
    set BLUEBUBBLES_WEBHOOK_SECRET in production.
    """
    secret = _webhook_secret()
    if not secret:
        return
    provided = request.query_params.get("key", "")
    if not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=403, detail="Invalid webhook key.")


@router.post("/webhooks/bluebubbles")
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
    chat_guid = ev.pop("_chat_guid", "")
    if ev.get("address"):
        ratelimit.enforce_sender(f"bluebubbles:{ev['address']}")
    if ev.get("unsupported_message") == "media":
        send_message(
            ev["address"],
            "I got the attachment, but I work with text — paste the "
            "messages, or send the person's contact card.",
            chat_guid=chat_guid,
        )
        return {"status": "ok"}
    if ev.get("unsupported_message") == "contact_card":
        send_message(
            ev["address"],
            "I couldn't read that contact card — type the person's "
            "number instead and I'll add it.",
            chat_guid=chat_guid,
        )
        return {"status": "ok"}
    owner_id = store.owner_id_for_address("bluebubbles", ev["address"])
    for reply in agent.handle_event(ev):
        try:
            send_message(ev["address"], reply["text"], reply.get("buttons"), chat_guid=chat_guid)
        except Exception:
            # The reply matters (it may be a match/flag notice): queue
            # it for the next delivery instead of losing it.
            store.enqueue_outbox(
                owner_id, "bluebubbles", ev["address"],
                reply["text"], reply.get("buttons"),
            )
    # Deliver anything the agent owes other users (match/flag notices).
    # Peek + mark-after-send: a failed send stays queued for next time.
    for row in store.peek_outbox(channel="bluebubbles"):
        try:
            send_message(row["address"], row["text"], row["buttons"])
        except Exception:
            continue
        store.mark_outbox_sent([row["id"]])
    return {"status": "ok"}
