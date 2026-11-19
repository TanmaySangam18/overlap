"""OVERLAP — a privacy-conscious matching service that lives in chat.

There is no OVERLAP app. Users talk to the agent inside WhatsApp (an
official Business number), Telegram (a BotFather bot), or inside
Messages (an SMS agent number), and an iPhone Shortcuts automation
feeds it from iMessage while a Telethon user client feeds it from the
user's own Telegram account. Signals arrive only from the user's own
actions: messages they forward or paste, contact cards they share once
per person, chat exports they send, and their own phone's automation
or their own Telegram client.

It never reads a chat the user did not send it, does not access another
person's account or device, does not decide that anyone cheated, and
never messages third parties about a match. Alerts are internal records
that can only be created after the user explicitly reviews and confirms.
"""

import hmac
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import agent, evidence, export_parser, flagging, legal, ratelimit, store
from .normalize import normalize_identifier
from .bluebubbles import router as bluebubbles_router
from .sendblue import router as sendblue_router
from .sms import router as sms_router
from .telegram import router as telegram_router
from .whatsapp import router as whatsapp_router

_WEB_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"

DISCLAIMER = "A potential overlap is not proof of a relationship or misconduct."

# The interactive docs and OpenAPI schema are a DEV convenience only:
# they are served solely when OVERLAP_ENV=dev is set explicitly. In
# every other environment (including a misconfigured one) the public
# surface is the site, the legal pages, and the webhooks — fail closed.
_DEV = os.environ.get("OVERLAP_ENV") == "dev"

app = FastAPI(
    title="OVERLAP",
    version="0.2.0",
    description=(
        "Chat-agent surfaces (WhatsApp bot, Telegram bot, SMS agent, "
        "iPhone Shortcuts ingest, Telegram user-client ingest) over "
        "user-submitted message signals, sender-overlap matching, and "
        "review-gated alerts. The bots never read a chat the user did "
        "not send them; the Telegram client reads only the account its "
        "owner logged into, with that owner's consent."
    ),
    docs_url="/docs" if _DEV else None,
    redoc_url="/redoc" if _DEV else None,
    openapi_url="/openapi.json" if _DEV else None,
)
app.include_router(whatsapp_router)
app.include_router(sendblue_router)
app.include_router(bluebubbles_router)
app.include_router(sms_router)
app.include_router(telegram_router)


# --- Daily maintenance ---------------------------------------------------
# Two jobs used to exist only as operator scripts, which meant they
# never ran in production: the retention purge (privacy promise) and
# the evidence-chain timestamp anchor. They now run in-process: once
# shortly after boot, then every 24h. Both are idempotent and failure-
# tolerant (a failed anchor submission stays pending and retries on
# the next run; the purge is a no-op unless OVERLAP_RETENTION_DAYS is
# set). OVERLAP_MAINTENANCE_OFF=1 disables the loop (tests, scripts).


def _run_maintenance_once() -> dict:
    result = {}
    try:
        result["purge"] = store.purge_old()
    except Exception as exc:  # never let maintenance kill the loop
        result["purge_error"] = str(exc)
    try:
        anchor = evidence.run_anchor()
        result["anchor"] = {
            "root": anchor["root"],
            "attempts": len(anchor["attempts"]),
        }
    except Exception as exc:
        result["anchor_error"] = str(exc)
    return result


@app.on_event("startup")
async def _start_maintenance() -> None:
    if os.environ.get("OVERLAP_MAINTENANCE_OFF"):
        return
    import asyncio

    async def loop() -> None:
        while True:
            await asyncio.to_thread(_run_maintenance_once)
            await asyncio.sleep(24 * 3600)

    asyncio.create_task(loop())


# --- Owner-scoped API authorization -------------------------------------
# The REST endpoints that read or write one owner's data (/signals,
# /matches...) are NOT public surface. Owner ids are derivable (chat
# owner ids are a truncated hash of channel:address), so possession of
# an owner id proves nothing. Access requires either the operator
# token (OVERLAP_API_TOKEN, header X-Overlap-Admin) or a valid ingest
# token belonging to that owner (header X-Overlap-Token) — the same
# credential the ingest endpoints already use.


def _api_token() -> str:
    return os.environ.get("OVERLAP_API_TOKEN", "")


def _is_admin(provided: str) -> bool:
    expected = _api_token()
    return bool(expected) and bool(provided) and hmac.compare_digest(provided, expected)


def _authorize_owner(owner_id: str, x_overlap_token: str, x_overlap_admin: str) -> None:
    if _is_admin(x_overlap_admin):
        return
    if x_overlap_token and store.owner_for_token(x_overlap_token) == owner_id:
        return
    raise HTTPException(
        status_code=401,
        detail="A valid ingest token for this owner, or the operator token, is required.",
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


@app.get("/healthz")
def healthz():
    """Liveness + database reachability, for the host's health checks."""
    if store.db_reachable():
        return {"status": "ok", "db": "ok"}
    return JSONResponse(
        status_code=503, content={"status": "degraded", "db": "unreachable"}
    )


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return legal.PRIVACY_HTML


@app.get("/terms", response_class=HTMLResponse)
def terms_page():
    return legal.TERMS_HTML


class SignalIn(BaseModel):
    owner_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    sender_identifier: str = Field(min_length=1)
    message_text: str = ""
    sent_at: datetime
    consent_confirmed: bool = False


class ReviewIn(BaseModel):
    owner_id: str = Field(min_length=1)
    decision: str = Field(pattern="^(confirmed|dismissed)$")


class AlertIn(BaseModel):
    owner_id: str = Field(min_length=1)


class TokenIn(BaseModel):
    owner_id: str = Field(min_length=1)


class ShortcutIngestIn(BaseModel):
    sender_identifier: str = Field(min_length=1)
    message_text: str = ""
    sent_at: datetime | None = None
    conversation_id: str = "imessage"


class ScreenshotIngestIn(BaseModel):
    text: str = Field(min_length=1)
    source_app: str | None = None


def _match_key(identifier_hash: str) -> str:
    return identifier_hash[:16]


def _build_match(identifier_hash: str, viewer_owner_id: str):
    """Return the potential-overlap view for one viewer, or None.

    A match exists only when the same sender identifier was submitted by at
    least two different owners. Other owners are never identified; evidence
    is limited to source, conversation, timestamp, and the stored snippet.
    """
    signals = store.signals_for_hash(identifier_hash)
    owners = {s["owner_id"] for s in signals}
    if len(owners) < 2:
        return None
    key = _match_key(identifier_hash)
    review = store.get_review(key, viewer_owner_id)
    evidence = [
        {
            "source": s["source"],
            "conversation_id": s["conversation_id"],
            "sent_at": s["sent_at"],
            "message_snippet": s["message_snippet"],
            "submitted_by": "you" if s["owner_id"] == viewer_owner_id else "another user",
        }
        for s in signals
    ]
    return {
        "match_id": f"match_{key}",
        "identifier": {
            "type": signals[0]["identifier_type"],
            "masked": signals[0]["identifier_masked"],
        },
        "distinct_owner_count": len(owners),
        "signal_count": len(signals),
        "first_seen": min(s["sent_at"] for s in signals),
        "last_seen": max(s["sent_at"] for s in signals),
        "evidence": evidence,
        "your_review": review["decision"] if review else None,
        "disclaimer": DISCLAIMER,
    }


def _require_participant(identifier_hash: str, owner_id: str):
    signals = store.signals_for_hash(identifier_hash)
    if not any(s["owner_id"] == owner_id for s in signals):
        raise HTTPException(status_code=404, detail="Match not found for this user.")
    return signals


@app.get("/")
def home():
    """The live site: landing page + self-serve two-person test."""
    return FileResponse(_WEB_INDEX)


@app.get("/cafe-bg.jpg")
def cafe_bg():
    return FileResponse(_WEB_INDEX.parent / "cafe-bg.jpg", media_type="image/jpeg")


@app.get("/cafe-loop.mp4")
def cafe_loop():
    """The landing page's looping café backdrop video."""
    return FileResponse(_WEB_INDEX.parent / "cafe-loop.mp4", media_type="video/mp4")


@app.get("/api")
def root():
    return {
        "service": "OVERLAP",
        "status": "ok",
        "docs": "/docs" if _DEV else None,
        "disclaimer": DISCLAIMER,
    }


@app.post("/signals", status_code=201)
def submit_signal(
    payload: SignalIn,
    x_overlap_token: str = Header(default=""),
    x_overlap_admin: str = Header(default=""),
):
    _authorize_owner(payload.owner_id, x_overlap_token, x_overlap_admin)
    if not payload.consent_confirmed:
        raise HTTPException(
            status_code=400,
            detail="consent_confirmed must be true: only submit messages you are authorized to share.",
        )
    try:
        ident = normalize_identifier(payload.sender_identifier)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    signal_id = store.add_signal(
        owner_id=payload.owner_id,
        source=payload.source,
        conversation_id=payload.conversation_id,
        ident=ident,
        message_text=payload.message_text,
        sent_at_iso=payload.sent_at.isoformat(),
    )
    return {
        "signal_id": signal_id,
        "identifier": {"type": ident["type"], "masked": ident["masked"]},
        "potential_match": _build_match(ident["hash"], payload.owner_id),
        "disclaimer": DISCLAIMER,
    }


@app.post("/tokens", status_code=201)
def create_ingest_token(
    payload: TokenIn,
    x_overlap_admin: str = Header(default=""),
):
    """Issue the personal token a user's iOS Shortcuts automation presents.

    Operator-only: a token is a standing credential for its owner, so
    minting one for an arbitrary owner id would be impersonation.
    Requires OVERLAP_API_TOKEN (header X-Overlap-Admin); when the
    operator token is not configured, minting is disabled entirely.
    """
    if not _api_token():
        raise HTTPException(
            status_code=503,
            detail="Token minting is disabled: OVERLAP_API_TOKEN is not configured.",
        )
    if not _is_admin(x_overlap_admin):
        raise HTTPException(status_code=401, detail="Operator token required.")
    token = store.create_ingest_token(payload.owner_id)
    return {
        "owner_id": payload.owner_id,
        "ingest_token": token,
        "ingest_url": "/ingest/shortcut",
        "note": "Paste this token into the OVERLAP Capture shortcut as the "
        "X-Overlap-Token header. Treat it like a password.",
    }


@app.post("/ingest/shortcut", status_code=201)
def ingest_shortcut(
    payload: ShortcutIngestIn,
    request: Request,
    x_overlap_token: str = Header(default=""),
):
    """Receive one message from a user's own iOS Shortcuts automation.

    The automation runs on the user's iPhone (Apple's Messages trigger) and
    posts the sender + text of each new message it is scoped to. This turns
    the message into a normal signal with source 'imessage'.
    """
    ratelimit.enforce(request, f"ingest:{x_overlap_token}")
    owner_id = store.owner_for_token(x_overlap_token)
    if not owner_id:
        raise HTTPException(status_code=401, detail="Invalid ingest token.")
    try:
        ident = normalize_identifier(payload.sender_identifier)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    sent_at = payload.sent_at or datetime.now(timezone.utc)
    signal_id = store.add_signal(
        owner_id=owner_id,
        source="imessage",
        conversation_id=payload.conversation_id,
        ident=ident,
        message_text=payload.message_text,
        sent_at_iso=sent_at.isoformat(),
    )
    return {
        "signal_id": signal_id,
        "ingested_via": "ios_shortcuts_automation",
        "identifier": {"type": ident["type"], "masked": ident["masked"]},
        "potential_match": _build_match(ident["hash"], owner_id),
        "disclaimer": DISCLAIMER,
    }


class TelegramIngestIn(BaseModel):
    sender_identifier: str = Field(min_length=1)
    message_text: str = ""
    sent_at: datetime | None = None
    conversation_id: str = "telegram-client"


@app.post("/ingest/telegram", status_code=201)
def ingest_telegram(
    payload: TelegramIngestIn,
    request: Request,
    x_overlap_token: str = Header(default=""),
):
    """Receive one message from a user's own Telegram user client.

    The client runs on the user's own machine, logged into their own
    Telegram account (Telethon, Telegram's sanctioned third-party-client
    API — see scripts/telegram_client.py), and posts each new message in
    their own cloud chats here. This is the automatic Telegram tier:
    log in once, and capture runs by itself, like the iPhone Shortcuts
    automation does for iMessage. Secret chats are invisible to the
    client by design.

    The sender identifier is a phone number when Telegram shows one,
    or "telegram:<user id>" when it is hidden — both normalize to the
    same hash-matched identifier the bot route uses, so a person
    registered from a Telegram contact card and a person seen by this
    client match.
    """
    ratelimit.enforce(request, f"ingest:{x_overlap_token}")
    owner_id = store.owner_for_token(x_overlap_token)
    if not owner_id:
        raise HTTPException(status_code=401, detail="Invalid ingest token.")
    try:
        ident = normalize_identifier(payload.sender_identifier)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    sent_at = payload.sent_at or datetime.now(timezone.utc)
    signal_id = store.add_signal(
        owner_id=owner_id,
        source="telegram",
        conversation_id=payload.conversation_id,
        ident=ident,
        message_text=payload.message_text,
        sent_at_iso=sent_at.isoformat(),
    )
    return {
        "signal_id": signal_id,
        "ingested_via": "telegram_user_client",
        "identifier": {"type": ident["type"], "masked": ident["masked"]},
        "potential_match": _build_match(ident["hash"], owner_id),
        "disclaimer": DISCLAIMER,
    }


@app.post("/ingest/screenshot", status_code=201)
def ingest_screenshot(
    payload: ScreenshotIngestIn,
    request: Request,
    x_overlap_token: str = Header(default=""),
):
    """Receive one chat screenshot's OCR text from the user's iPhone.

    The OVERLAP Screenshot shortcut OCRs the image on-device (Shortcuts'
    Extract Text) and posts only the text — the image never leaves the
    phone. Any app's chat works, because every app shows its chats on
    screen. Attribution, filing, and confirmation follow the same rules
    as forwarded messages (see agent.ingest_screenshot_for_api); filed
    snippets carry source 'screenshot'.
    """
    ratelimit.enforce(request, f"ingest:{x_overlap_token}")
    owner_id = store.owner_for_token(x_overlap_token)
    if not owner_id:
        raise HTTPException(status_code=401, detail="Invalid ingest token.")
    result = agent.ingest_screenshot_for_api(
        owner_id, payload.text, payload.source_app
    )
    if result["status"] == "no_person":
        raise HTTPException(
            status_code=422,
            detail="Nobody is registered for this user yet — share a "
            "contact card with OVERLAP first, then send the screenshot.",
        )
    person = result["person"]
    ident = {
        "type": person["identifier_type"],
        "hash": person["identifier_hash"],
        "masked": person["identifier_masked"],
    }
    return {
        "ingested_via": "ios_shortcut_screenshot",
        "status": result["status"],
        "attribution": result["attribution"],
        "source_app": payload.source_app,
        "person": {
            "label": person["label"],
            "type": ident["type"],
            "masked": ident["masked"],
        },
        "filed": result["filed"],
        "staged": result["staged"],
        "duplicates_skipped": result["duplicates"],
        "potential_match": (
            _build_match(ident["hash"], owner_id)
            if result["status"] == "filed"
            else None
        ),
        "disclaimer": DISCLAIMER,
    }


@app.get("/matches")
def list_matches(
    owner_id: str = Query(min_length=1),
    x_overlap_token: str = Header(default=""),
    x_overlap_admin: str = Header(default=""),
):
    _authorize_owner(owner_id, x_overlap_token, x_overlap_admin)
    matches = []
    for identifier_hash in store.hashes_for_owner(owner_id):
        match = _build_match(identifier_hash, owner_id)
        if match:
            matches.append(match)
    return {"owner_id": owner_id, "matches": matches, "disclaimer": DISCLAIMER}


@app.get("/matches/{match_id}")
def get_match(
    match_id: str,
    owner_id: str = Query(min_length=1),
    x_overlap_token: str = Header(default=""),
    x_overlap_admin: str = Header(default=""),
):
    _authorize_owner(owner_id, x_overlap_token, x_overlap_admin)
    identifier_hash = store.find_hash_by_match_id(match_id)
    if not identifier_hash:
        raise HTTPException(status_code=404, detail="Match not found.")
    _require_participant(identifier_hash, owner_id)
    match = _build_match(identifier_hash, owner_id)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found.")
    return match


@app.post("/matches/{match_id}/review")
def review_match(
    match_id: str,
    payload: ReviewIn,
    x_overlap_token: str = Header(default=""),
    x_overlap_admin: str = Header(default=""),
):
    _authorize_owner(payload.owner_id, x_overlap_token, x_overlap_admin)
    identifier_hash = store.find_hash_by_match_id(match_id)
    if not identifier_hash:
        raise HTTPException(status_code=404, detail="Match not found.")
    _require_participant(identifier_hash, payload.owner_id)
    store.set_review(_match_key(identifier_hash), payload.owner_id, payload.decision)
    return {
        "match_id": match_id,
        "your_review": payload.decision,
        "alerts_enabled": payload.decision == "confirmed",
    }


@app.post("/matches/{match_id}/alert", status_code=201)
def create_alert(
    match_id: str,
    payload: AlertIn,
    x_overlap_token: str = Header(default=""),
    x_overlap_admin: str = Header(default=""),
):
    _authorize_owner(payload.owner_id, x_overlap_token, x_overlap_admin)
    identifier_hash = store.find_hash_by_match_id(match_id)
    if not identifier_hash:
        raise HTTPException(status_code=404, detail="Match not found.")
    _require_participant(identifier_hash, payload.owner_id)
    review = store.get_review(_match_key(identifier_hash), payload.owner_id)
    if not review or review["decision"] != "confirmed":
        raise HTTPException(
            status_code=409,
            detail="Explicit review required: confirm this match before creating an alert.",
        )
    alert_id = store.add_alert(_match_key(identifier_hash), payload.owner_id)
    return {
        "alert_id": alert_id,
        "match_id": match_id,
        "status": "recorded",
        "delivery": "Internal record only. No message was sent to any third party.",
        "disclaimer": DISCLAIMER,
    }



class ExportIn(BaseModel):
    partner_identifier: str = Field(min_length=1)
    owner_name: str = Field(min_length=1)
    export_text: str = Field(min_length=1)
    partner_label: str | None = None


def _flags_for_owner(owner_id):
    """Pattern-change reports for every relationship this owner protects.

    Reports never identify the other user — only that a pattern changed.
    """
    out = []
    for person in store.persons_for_owner(owner_id):
        pair = store.get_pair_state(owner_id, person["identifier_hash"])
        if not pair or pair["relationship_state"] != "together" or not pair["since_iso"]:
            continue
        ident = {
            "type": person["identifier_type"],
            "hash": person["identifier_hash"],
            "masked": person["identifier_masked"],
        }
        result = flagging.evaluate(owner_id, "", pair["since_iso"], partner_ident=ident)
        reports = [
            {"flagged": r["flagged"], "status": r["status"], "reasons": r["reasons"]}
            for r in result["reports"]
        ]
        if any(r["flagged"] for r in reports):
            out.append(
                {
                    "partner_label": person["label"],
                    "partner_masked": person["identifier_masked"],
                    "since": pair["since_iso"],
                    "reports": reports,
                }
            )
    return out


def _run_export_ingest(owner_id, partner_identifier, owner_name, export_text, partner_label):
    try:
        ident = normalize_identifier(partner_identifier)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    parsed = export_parser.parse_export(export_text)
    if not parsed["messages"]:
        raise HTTPException(
            status_code=422, detail="No messages found in that export text."
        )
    others = [n for n in parsed["names"] if n != owner_name]
    if len(others) != 1:
        raise HTTPException(
            status_code=422,
            detail="Export must be a 1:1 chat and owner_name must match the "
            "name your messages carry in it.",
        )
    label = partner_label or others[0]
    store.add_person(owner_id, label, ident)
    person = store.person_by_hash(owner_id, ident["hash"])
    replies = agent.ingest_export_for_api(owner_id, parsed, person, owner_name)
    return {
        "owner_id": owner_id,
        "partner": {"label": label, "type": ident["type"], "masked": ident["masked"]},
        "agent_replies": [r["text"] for r in replies],
        "relationship": store.get_pair_state(owner_id, ident["hash"]),
        "potential_match": _build_match(ident["hash"], owner_id),
        "flags": _flags_for_owner(owner_id),
        "disclaimer": DISCLAIMER,
    }


@app.post("/ingest/export", status_code=201)
def ingest_export(
    payload: ExportIn,
    request: Request,
    x_overlap_token: str = Header(default=""),
):
    """Ingest a WhatsApp chat export for a token holder (no Meta needed).

    This is the same parser + dedupe + relationship detection the WhatsApp
    bot uses; the endpoint exists so exports can be tested and used before
    the business number is live. The partner is registered for the owner
    ('once per person, ever') from partner_identifier.
    """
    ratelimit.enforce(request, f"ingest:{x_overlap_token}")
    owner_id = store.owner_for_token(x_overlap_token)
    if not owner_id:
        raise HTTPException(status_code=401, detail="Invalid ingest token.")
    return _run_export_ingest(
        owner_id,
        payload.partner_identifier,
        payload.owner_name,
        payload.export_text,
        payload.partner_label,
    )


MAX_EXPORT_BYTES = 10 * 1024 * 1024  # 10 MB upload cap for chat exports


@app.post("/ingest/export-file", status_code=201)
async def ingest_export_file(
    request: Request,
    file: UploadFile = File(...),
    partner_identifier: str = Form(...),
    owner_name: str = Form(...),
    partner_label: str | None = Form(default=None),
    x_overlap_token: str = Header(default=""),
):
    """The live site's upload: the raw export file (.txt or .zip)."""
    ratelimit.enforce(request, f"ingest:{x_overlap_token}")
    owner_id = store.owner_for_token(x_overlap_token)
    if not owner_id:
        raise HTTPException(status_code=401, detail="Invalid ingest token.")
    if file.size is not None and file.size > MAX_EXPORT_BYTES:
        raise HTTPException(status_code=413, detail="Export file is too large (10 MB max).")
    data = await file.read()
    if len(data) > MAX_EXPORT_BYTES:
        raise HTTPException(status_code=413, detail="Export file is too large (10 MB max).")
    try:
        export_text = export_parser.extract_export_text(data)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail="That file is not a WhatsApp chat export (.txt or .zip).",
        ) from exc
    return _run_export_ingest(
        owner_id, partner_identifier, owner_name, export_text, partner_label
    )


@app.get("/ingest/whoami")
def ingest_whoami(x_overlap_token: str = Header(default="")):
    owner_id = store.owner_for_token(x_overlap_token)
    if not owner_id:
        raise HTTPException(status_code=401, detail="Invalid ingest token.")
    return {"owner_id": owner_id}


@app.get("/ingest/flags")
def ingest_flags(x_overlap_token: str = Header(default="")):
    owner_id = store.owner_for_token(x_overlap_token)
    if not owner_id:
        raise HTTPException(status_code=401, detail="Invalid ingest token.")
    return {
        "owner_id": owner_id,
        "flags": _flags_for_owner(owner_id),
        "disclaimer": DISCLAIMER,
    }


@app.get("/evidence")
def get_evidence_pack(
    request: Request,
    person: str = Query(min_length=1),
    x_overlap_token: str = Header(default=""),
):
    """Download your tamper-evident evidence pack for one person.

    Token-authenticated like the other ingest endpoints. `person` is a
    display name or masked id, resolved against YOUR registered persons
    only. The pack contains only your own submissions about that person
    (with their hash-chain links), the chain verification, the public
    timestamp anchors covering the stream, per-source strength counts,
    and — if a match exists — dates and counts only. The other side's
    content is never in it.
    """
    ratelimit.enforce(request, f"ingest:{x_overlap_token}")
    owner_id = store.owner_for_token(x_overlap_token)
    if not owner_id:
        raise HTTPException(status_code=401, detail="Invalid ingest token.")
    person_row = store.person_by_label(owner_id, person)
    if person_row is None:
        for candidate in store.persons_for_owner(owner_id):
            if candidate["identifier_masked"] == person.strip():
                person_row = candidate
                break
    if person_row is None:
        raise HTTPException(
            status_code=404,
            detail="No registered person matches that name or masked id.",
        )
    return evidence.build_evidence_pack(owner_id, person_row)


@app.post("/evidence/verify")
async def verify_evidence_pack(request: Request):
    """Public, stateless pack verification: recompute the chain.

    No auth — anyone holding a pack can check it, and nothing about the
    pack is stored. The body is the pack itself, exactly as GET
    /evidence returned it.
    """
    ratelimit.enforce(request, "evidence:verify")
    try:
        pack = await request.json()
    except Exception:
        pack = None
    if not isinstance(pack, dict):
        return {
            "chain_intact": False,
            "entries_checked": 0,
            "anchor_status": "none",
            "anchor_verified": False,
        }
    return evidence.verify_pack(pack)


@app.get("/verify", response_class=HTMLResponse)
def verify_page():
    """The public paste-a-pack verification page (see app/evidence.py)."""
    return evidence.VERIFY_PAGE_HTML
