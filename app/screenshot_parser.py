"""Parser for chat screenshots OCR'd on the user's own iPhone.

The screenshot is the one surface every chat app must expose: WhatsApp,
Instagram, Tinder, anything. The user screenshots a chat, shares it to
the OVERLAP Screenshot shortcut, and the shortcut's on-device OCR
(Shortcuts' "Extract Text from Image") posts the raw text here. The
image itself never leaves the phone.

The input is messy: many lines of OCR text. The chat partner's display
name sits near the top as the chat header; message lines follow; and
there is UI noise — standalone timestamps, "online", "last seen today
at…", "typing…", date dividers. The heuristics here are deliberately
conservative:

Attribution
- Only the first HEADER_SCAN_LINES lines are searched for a registered
  person's display name (exact line, first-name line, or whole-word
  substring). A match is a CONFIRMED attribution.
- With no header match, the parser falls back to the owner's most
  recently active person — the burst-attribution rule the agent uses
  for forwards (last attributed person, else latest signal activity,
  else most recently registered) — and marks the result UNCONFIRMED.
  Unconfirmed screenshots are never filed silently: the agent stages
  them in its normal pending/attribution flow and asks the user to tap
  the right name (see agent.ingest_screenshot_for_api).

Extraction
- The matched header line itself is dropped (it is the name, not a
  message). An unmatched header line is NOT dropped — there is no
  reliable way to tell it from a short first message.
- Only lines that are clearly UI noise are dropped (see _is_noise).
  Anything sentence-like is kept, one surviving line per snippet:
  a wrapped bubble can arrive as several snippets. That is accepted —
  snippets are evidence fragments everywhere else in OVERLAP too.
- OCR text carries no direction (no left/right bubbles) and no usable
  per-message times, so extracted messages are stamped with the ingest
  time and filed as incoming from the attributed person — the same
  treatment forwarded messages get. The user's own visible messages in
  the shot can therefore be filed as the partner's; SCREENSHOT-
  SHORTCUT.md states this limit plainly.

Dedupe
- Each candidate is fingerprinted by the normalized form of the
  snippet exactly as it would be stored (raw text truncated to
  store.SNIPPET_LEN, then lowercased with punctuation collapsed).
- A candidate is a duplicate if its fingerprint matches an existing
  pair message for (owner, person) or an earlier line of the same
  screenshot, so overlapping screenshots never double-count. For an
  unconfirmed screenshot the check runs against the guessed person's
  history — the person it will almost always be filed under.
"""

import hashlib
import re
from datetime import datetime, timezone

from . import store

HEADER_SCAN_LINES = 5
MAX_MESSAGES_PER_SCREENSHOT = 50

_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?\s*([AP]M)?$", re.IGNORECASE)
_SLASH_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}(/\d{2,4})?$")
_MONTH_DATE_RE = re.compile(
    r"^([A-Z][a-z]+ \d{1,2}(, \d{4})?|\d{1,2} [A-Z][a-z]+( \d{4})?)$"
)
_WEEKDAY_RE = re.compile(
    r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)(,.*)?$",
    re.IGNORECASE,
)
_UNREAD_RE = re.compile(r"^\d*\s*unread messages?$", re.IGNORECASE)

_NOISE_EXACT = {
    "online",
    "online now",
    "active now",
    "active today",
    "typing",
    "typing...",
    "typing…",
    "today",
    "yesterday",
    "delivered",
    "seen",
    "read",
    "message",
    "5g",
    "lte",
    "4g",
}


def _is_noise(line: str) -> bool:
    """True only for lines that are clearly chat-UI furniture."""
    low = line.strip().lower()
    if not low:
        return True
    if low in _NOISE_EXACT:
        return True
    if low.startswith("last seen"):
        return True
    if "end-to-end encrypted" in low:
        return True
    if _TIME_RE.match(low) or _SLASH_DATE_RE.match(low):
        return True
    if _WEEKDAY_RE.match(low) or _UNREAD_RE.match(low):
        return True
    if _MONTH_DATE_RE.match(line.strip()):
        return True
    return False


def _fingerprint(text: str) -> str:
    """Normalized-snippet hash used for dedupe (see module docstring)."""
    stored_form = (text or "")[: store.SNIPPET_LEN]
    normalized = re.sub(r"[^a-z0-9]+", " ", stored_form.lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _match_header_person(persons, lines):
    """Search the header zone for a registered person's display name.

    Returns (person, line_index) for the earliest matching line — within
    one line, the longest label wins — or (None, None).
    """
    for idx, raw in enumerate(lines[:HEADER_SCAN_LINES]):
        line = raw.strip().lower()
        if not line:
            continue
        best = None
        for person in persons:
            label = person["label"].strip().lower()
            if not label:
                continue
            first = label.split(" ")[0]
            if (
                line == label
                or line == first
                or re.search(rf"\b{re.escape(label)}\b", line)
            ):
                if best is None or len(label) > len(best["label"]):
                    best = person
        if best is not None:
            return best, idx
    return None, None


def _burst_person(owner_id, persons, ctx):
    """Most recently active person: the burst-attribution fallback."""
    last_hash = (ctx or {}).get("last_person_hash")
    if last_hash:
        person = store.person_by_hash(owner_id, last_hash)
        if person is not None:
            return person
    best, best_at = None, ""
    for person in persons:
        for sig in store.signals_for_hash(person["identifier_hash"]):
            if sig["owner_id"] == owner_id and sig["sent_at"] > best_at:
                best, best_at = person, sig["sent_at"]
    if best is not None:
        return best
    return persons[-1]  # most recently registered


def parse_screenshot(owner_id, ocr_text):
    """Parse raw OCR text of one chat screenshot for one owner.

    Returns {
        "person": person row or None (None only when the owner has
                  nobody registered),
        "confirmed": True when the header name matched,
        "messages": [{"text", "sent_at"}...] — new messages only,
        "duplicates": int, "noise_lines": int,
    }
    """
    lines = [ln.strip() for ln in (ocr_text or "").splitlines()]
    persons = store.persons_for_owner(owner_id)
    if not persons:
        return {
            "person": None,
            "confirmed": False,
            "messages": [],
            "duplicates": 0,
            "noise_lines": 0,
        }

    state = store.get_agent_state(owner_id)
    ctx = state["context"]
    person, header_idx = _match_header_person(persons, lines)
    confirmed = person is not None
    if person is None:
        person = _burst_person(owner_id, persons, ctx)

    # Dedupe universe: this person's stored pair messages, plus any
    # screenshot snippets already staged for this owner (an unconfirmed
    # screenshot re-sent before confirmation must not stage twice).
    seen = {
        _fingerprint(row["message_snippet"])
        for row in store.pair_messages_for(owner_id, person["identifier_hash"])
    }
    for entry in ctx.get("pending", []):
        if entry.get("source") == "screenshot":
            seen.add(_fingerprint(entry.get("text", "")))

    now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    messages = []
    duplicates = 0
    noise_lines = 0
    batch = set()
    for idx, line in enumerate(lines):
        if idx == header_idx or _is_noise(line):
            noise_lines += 1
            continue
        fp = _fingerprint(line)
        if fp in seen or fp in batch:
            duplicates += 1
            continue
        batch.add(fp)
        messages.append({"text": line, "sent_at": now_iso})
        if len(messages) >= MAX_MESSAGES_PER_SCREENSHOT:
            break
    return {
        "person": person,
        "confirmed": confirmed,
        "messages": messages,
        "duplicates": duplicates,
        "noise_lines": noise_lines,
    }
