"""SMS transport for the OVERLAP agent — the Messages-app agent.

There is no self-serve "iMessage bot" API: a blue-bubble agent requires
Apple Messages for Business approval (via an authorized messaging
provider) or an always-on Mac relay. The route that works today, with
official APIs and no app install, is a real phone number users text —
on an iPhone that conversation lives in the Messages app next to their
iMessages (green bubble), while iMessage *intake* is covered by the
Shortcuts automation (see SHORTCUT.md).

This router speaks the Twilio webhook shape (form-encoded POST:
From / Body / MessageSid) and answers with TwiML. Point a Twilio (or
compatible) number's messaging webhook at POST /webhooks/sms.
Buttons don't exist in SMS, so replies render their options as text:
attribution is answered with the person's name, reviews with
CONFIRM / DISMISS — the engine understands both.
"""

from datetime import datetime, timezone
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request
from fastapi.responses import Response

from . import agent, ratelimit, store

router = APIRouter()


def _render(reply) -> str:
    text = reply["text"]
    buttons = reply.get("buttons") or []
    if buttons:
        ids = [b.get("id", "") for b in buttons]
        if any(i.startswith("review:") for i in ids):
            text += "\n(Reply CONFIRM or DISMISS)"
        elif any(i.startswith("attr:") for i in ids):
            names = " / ".join(b["title"] for b in buttons)
            text += f"\n(Reply with the name: {names})"
        elif any(i == "consent:yes" for i in ids):
            text += "\n(Reply YES)"
    return text


def _twiml(messages) -> Response:
    inner = "".join(f"<Message>{escape(m)}</Message>" for m in messages)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>'
    return Response(content=xml, media_type="application/xml")


@router.post("/webhooks/sms")
async def receive(request: Request):
    # Twilio posts application/x-www-form-urlencoded; parse it directly so
    # the service needs no extra form-parsing dependency.
    raw = (await request.body()).decode("utf-8", errors="replace")
    form = {k: v[0] for k, v in parse_qs(raw).items()}
    address = form.get("From", "")
    ratelimit.enforce(request, f"sms:{address}")
    ev = {
        "channel": "sms",
        "address": address,
        "display_name": None,
        "kind": "text",
        "text": form.get("Body", ""),
        "contact": None,
        "document_bytes": None,
        "button_id": None,
        "message_id": form.get("MessageSid"),
        "sent_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    }
    replies = agent.handle_event(ev)
    texts = [_render(r) for r in replies]
    # Anything the agent owes this user (notices queued while others were
    # chatting) rides along with their next text.
    owner_id = store.owner_id_for_address("sms", address)
    for row in store.drain_outbox(channel="sms", owner_id=owner_id):
        texts.append(_render({"text": row["text"], "buttons": row["buttons"]}))
    return _twiml(texts)
