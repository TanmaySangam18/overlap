"""Relationship-event detection over a pair's submitted message timeline.

The point: a user should not have to declare "we are dating". If one person
confesses feelings or proposes in the messages themselves, OVERLAP notes the
event — and notes the other person's reply, because the reply is what makes
it a relationship:

- confession + acceptance -> state "together", starting at the confession
- confession + rejection  -> state "not_together"
- confession + no clear reply -> state "unconfirmed" (noted, never alerted on)

Detection is phrase-based and explainable on purpose. A bare "ily" or
"i love you" is NOT treated as a confession — between friends it is often
their normal (it is tracked separately as an affection marker in flagging).
Only explicit romantic-declaration phrases count as a confession event.

Timeline entries are dicts: {"from": <party label>, "text": str,
"sent_at": ISO-8601 str}. The timeline is built from submitted signals in
both directions (each person's phone captures the other person's messages).
"""

import re
from datetime import datetime, timedelta

CONFESSION_PHRASES = [
    "will you marry me",
    "marry me",
    "will you be my girlfriend",
    "will you be my boyfriend",
    "be my girlfriend",
    "be my boyfriend",
    "i have feelings for you",
    "i've got feelings for you",
    "i'm in love with you",
    "im in love with you",
    "i am in love with you",
    "i've fallen for you",
    "ive fallen for you",
    "fallen for you",
    "i like you more than a friend",
    "more than a friend",
    "will you go out with me",
    "i want to be with you",
    "i want us to be together",
]

ACCEPT_MARKERS = [
    "i love you too",
    "me too",
    "i feel the same",
    "of course",
    "absolutely",
    "finally",
    "i'd love to",
    "id love to",
    "❤️",
    "😍",
]

REJECT_MARKERS = [
    "just friends",
    "just be friends",
    "only friends",
    "don't feel the same",
    "dont feel the same",
    "not interested",
    "just want to be friends",
    "see you as a friend",
    "like a brother",
    "like a sister",
    "i can't be with you",
    "i cant be with you",
]

REPLY_WINDOW = timedelta(hours=72)


def _contains_any(text: str, phrases) -> bool:
    t = (text or "").lower()
    return any(p in t for p in phrases)


def _says_yes(text: str) -> bool:
    return re.search(r"\byes\b", (text or "").lower()) is not None


def classify_reply(text: str) -> str:
    """Classify one reply message: 'accepted', 'rejected', or 'unclear'.

    Rejection is checked first: a polite message that contains a rejection
    marker is a rejection even if it also contains friendly words.
    """
    if _contains_any(text, REJECT_MARKERS):
        return "rejected"
    if _contains_any(text, ACCEPT_MARKERS) or _says_yes(text):
        return "accepted"
    return "unclear"


def detect_relationship(timeline):
    """Find the first confession event and classify the reply to it."""
    ordered = sorted(timeline, key=lambda m: m["sent_at"])
    for i, msg in enumerate(ordered):
        if not _contains_any(msg["text"], CONFESSION_PHRASES):
            continue
        event_at = datetime.fromisoformat(msg["sent_at"])
        event = {
            "type": "confession",
            "by": msg["from"],
            "at": msg["sent_at"],
            "snippet": msg["text"][:80],
        }
        reply = None
        for other in ordered[i + 1 :]:
            if other["from"] == msg["from"]:
                continue
            if datetime.fromisoformat(other["sent_at"]) - event_at > REPLY_WINDOW:
                break
            verdict = classify_reply(other["text"])
            if verdict != "unclear":
                reply = {
                    "classification": verdict,
                    "by": other["from"],
                    "at": other["sent_at"],
                    "snippet": other["text"][:80],
                }
                break
            if reply is None:
                reply = {
                    "classification": "unclear",
                    "by": other["from"],
                    "at": other["sent_at"],
                    "snippet": other["text"][:80],
                }
        if reply and reply["classification"] == "accepted":
            state, since = "together", msg["sent_at"]
        elif reply and reply["classification"] == "rejected":
            state, since = "not_together", None
        else:
            state, since = "unconfirmed", None
        return {
            "detected": True,
            "event": event,
            "reply": reply,
            "state": state,
            "since": since,
        }
    return {"detected": False, "event": None, "reply": None, "state": None, "since": None}
