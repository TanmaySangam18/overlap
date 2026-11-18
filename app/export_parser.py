"""Parser for WhatsApp chat exports (.txt, optionally zipped).

WhatsApp's built-in "Export Chat" produces one text file. Two layouts are
in the wild:

- iOS:     [8/24/26, 9:41:07 PM] Peter Parker: message text
- Android: 8/24/26, 9:41 PM - Peter Parker: message text

Dates may be month-first (US) or day-first (most other locales); the order
is detected from the data itself (a component > 12 gives it away), with a
US month-first default. Times keep the phone's local clock — late-night
analysis wants local time, so timestamps are stored naive-local on purpose.

System lines (encryption notices, group edits, calls) are skipped. Media
and deleted messages are kept as empty-text entries: they carry no content
to analyze, but they are real messages and belong in frequency counts.
"""

import hashlib
import io
import re
import zipfile

_IOS_RE = re.compile(
    r"^\[(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<y>\d{2,4}),\s*"
    r"(?P<h>\d{1,2}):(?P<min>\d{2})(?::(?P<s>\d{2}))?\s*(?P<ampm>[AP]M)?\]\s*"
    r"(?P<rest>.*)$"
)
_ANDROID_RE = re.compile(
    r"^(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<y>\d{2,4}),\s*"
    r"(?P<h>\d{1,2}):(?P<min>\d{2})(?::(?P<s>\d{2}))?\s*(?P<ampm>[AP]M)?\s*-\s*"
    r"(?P<rest>.*)$"
)

_SYSTEM_PHRASES = [
    "end-to-end encrypted",
    "created group",
    "added you",
    "was added",
    "changed the subject",
    "changed this group's icon",
    "security code changed",
    "missed voice call",
    "missed video call",
    "tap to call back",
    "you were added",
    "left the group",
    "removed you",
]

_EMPTY_TEXT_MARKERS = {
    "<media omitted>",
    "image omitted",
    "video omitted",
    "audio omitted",
    "document omitted",
    "sticker omitted",
    "gif omitted",
    "this message was deleted",
    "you deleted this message",
}

_INVISIBLE = "‎‏﻿"


def extract_export_text(data: bytes) -> str:
    """Accept raw export bytes (.txt or .zip) and return the chat text."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
            if not names:
                raise ValueError("zip contains no .txt chat export")
            names.sort(key=lambda n: ("_chat" not in n.lower(), len(n)))
            return zf.read(names[0]).decode("utf-8", errors="replace")
    return data.decode("utf-8", errors="replace")


def dedupe_key(sent_at: str, sender: str, text: str) -> str:
    raw = f"{sent_at}|{sender}|{text}"
    return "exp_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _match_header(line: str):
    return _IOS_RE.match(line) or _ANDROID_RE.match(line)


def _detect_dayfirst(lines) -> bool:
    saw_monthfirst_proof = False
    for line in lines:
        m = _match_header(line)
        if not m:
            continue
        a, b = int(m.group("a")), int(m.group("b"))
        if a > 12:
            return True
        if b > 12:
            saw_monthfirst_proof = True
    return False if saw_monthfirst_proof else False  # default: US month-first


def _to_iso(m, dayfirst: bool) -> str:
    a, b, y = int(m.group("a")), int(m.group("b")), int(m.group("y"))
    day, month = (a, b) if dayfirst else (b, a)
    if y < 100:
        y += 2000
    hour = int(m.group("h"))
    minute = int(m.group("min"))
    second = int(m.group("s") or 0)
    ampm = m.group("ampm")
    if ampm:
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
    return f"{y:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"


def _is_system(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _SYSTEM_PHRASES)


def parse_export(text: str):
    """Parse export text into messages.

    Returns {"messages": [{"sent_at", "sender", "text"}...],
             "names": [sender names], "system_lines": int}
    """
    lines = [ln.strip(_INVISIBLE).rstrip("\n") for ln in text.splitlines()]
    dayfirst = _detect_dayfirst(lines)
    messages = []
    system_lines = 0

    for line in lines:
        m = _match_header(line)
        if m:
            rest = m.group("rest")
            if ": " in rest:
                sender, body = rest.split(": ", 1)
                sender = sender.strip()
                body = body.strip()
                if _is_system(body) and sender.lower() in ("", "whatsapp"):
                    system_lines += 1
                    continue
                if body.lower() in _EMPTY_TEXT_MARKERS:
                    body = ""
                messages.append(
                    {"sent_at": _to_iso(m, dayfirst), "sender": sender, "text": body}
                )
            else:
                # Header with no "Name: text" — a system event line.
                system_lines += 1
            continue
        # Continuation of a multi-line message.
        if messages and line:
            messages[-1]["text"] = (messages[-1]["text"] + "\n" + line).strip()

    names = sorted({msg["sender"] for msg in messages})
    return {"messages": messages, "names": names, "system_lines": system_lines}
