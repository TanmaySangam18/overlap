"""The OVERLAP chat agent — one brain, every chat surface.

OVERLAP is not an app. The user talks to it inside WhatsApp (bot number)
or inside Messages (SMS agent number), and the iPhone Shortcuts automation
feeds the same backend from iMessage. This module is the channel-agnostic
conversation engine; transports (app/whatsapp.py, app/sms.py) translate
platform payloads into events and render the replies.

An event is a dict:
    {channel, address, display_name, kind, text, contact, document_bytes,
     button_id, message_id, sent_at}
kind is one of: text | forwarded | contact | document | button.

Replies are dicts: {"text": str, "buttons": [{"id": ..., "title": ...}]}.

Rules carried over from the core product, unchanged:
- Consent first: nothing is stored before the user turns OVERLAP on.
- Identifiers are hashed + masked; the raw number is never stored.
- A match is a neutral "potential overlap", never a verdict.
- Alerts exist only after the user's explicit review (here: a tap).
- The other submitting user is never identified.
"""

import re
from datetime import datetime, timedelta, timezone

from . import events as events_mod
from . import evidence as evidence_mod
from . import export_parser, flagging, screenshot_parser, store
from .normalize import normalize_identifier

DISCLAIMER = "A potential overlap is not proof of a relationship or misconduct."
ATTRIBUTION_WINDOW = timedelta(hours=2)
MAX_EXPORT_MESSAGES = 5000
# Onboarding asks for contact cards up front (a bot cannot request the
# phone's contacts permission — there is no app to ask from — so the
# cards are the legal version of "who do you want me to watch first").
ONBOARDING_MAX_CARDS = 3
_DONE_BUTTON = {"id": "onboarding:done", "title": "Done — that's all"}

_YES = {"yes", "agree", "ok", "okay", "yes please", "turn it on"}

# Plain typed text is conversation with the agent, never a log entry —
# only forwards, exports and screenshots are evidence. These sets shape
# the conversational replies; the chat-shape check below decides when a
# paste looks like someone's messages and deserves a "log it?" ask.
_ACKS = {
    "thanks", "thank you", "ty", "ok", "okay", "cool", "great", "nice",
    "got it", "perfect", "awesome", "👍", "🙏", "👌",
}
_QUESTION_STARTERS = {
    "how", "what", "why", "when", "can", "could", "do", "does", "is",
    "are", "should", "will", "would", "who",
}
_STAMP_RE = re.compile(r"^(\[\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2})")
_NAMED_RE = re.compile(r"^[^:\n]{1,40}:\s+\S")


def _looks_like_chat(text):
    """Heuristic: does this paste look like lines from someone's chat?

    Long pastes, timestamped export lines, or several 'Name: message'
    lines count. A single typed sentence never does.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 4:
        return True
    if len(lines) < 2:
        return False
    if any(_STAMP_RE.match(ln) for ln in lines):
        return True
    return sum(1 for ln in lines if _NAMED_RE.match(ln)) >= 2


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ident_of_person(person):
    return {
        "type": person["identifier_type"],
        "hash": person["identifier_hash"],
        "masked": person["identifier_masked"],
    }


def _first_name(label: str) -> str:
    return (label or "").strip().split(" ")[0].lower()


def _find_person_for_name(owner_id, name):
    """Match an export sender name to a registered person (exact label,
    then first-name match)."""
    exact = store.person_by_label(owner_id, name)
    if exact:
        return exact
    for person in store.persons_for_owner(owner_id):
        if _first_name(person["label"]) == _first_name(name):
            return person
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def handle_event(ev):
    channel = ev["channel"]
    address = ev["address"]
    user = store.get_or_create_chat_user(channel, address, ev.get("display_name"))
    owner_id = user["owner_id"]

    # Platform retries must not double-ingest.
    message_id = ev.get("message_id")
    if message_id:
        if not store.mark_ingested(owner_id, f"msg_{channel}_{message_id}", "webhook"):
            return []

    if not user["consented_at"]:
        return _pre_consent(owner_id, ev)
    return _consented(owner_id, user, ev)


def _pre_consent(owner_id, ev):
    text = (ev.get("text") or "").strip().lower()
    if ev.get("button_id") == "consent:yes" or (ev["kind"] == "text" and text in _YES):
        store.set_consent(owner_id)
        store.set_agent_state(owner_id, "onboarding_cards", {})
        return [
            {
                "text": (
                    "You're in ✓\n\n"
                    "First, the people you want me to watch — share up to 3 "
                    "contact cards now (attach → Contact), one per person. "
                    "Once per person, ever: I remember each of them by a code "
                    "made from the number, not the number itself.\n\n"
                    "Then forward their messages here — or export your chat "
                    "with them (⋯ → Export Chat) and drop the file here for "
                    "instant history and a baseline.\n\n"
                    "Identifiers are stored as codes + masked, message text "
                    "as short snippets. Say 'forget me' any time and I delete "
                    "everything of yours.\n\n"
                    "Tap Done when you've shared them, or reply 'skip' to do "
                    "it later."
                ),
                "buttons": [_DONE_BUTTON],
            }
        ]
    # The bot shape is the same on every chat surface — the bot only
    # ever sees what the user sends it — but the exact reason differs,
    # so say it in that channel's own terms instead of naming WhatsApp
    # inside a Telegram chat (or vice versa).
    channel = ev.get("channel", "")
    if channel == "telegram":
        cannot_read = (
            "You send things to me here — I never read your other chats "
            "(a Telegram bot can't see them — only what you send me here)."
        )
    else:
        cannot_read = (
            "You send things to me here — I never read your other chats "
            "(WhatsApp doesn't allow that, for anyone)."
        )
    return [
        {
            "text": (
                "👋 I'm OVERLAP. I watch for one thing: the same person "
                "quietly showing up in two people's chats, and their pattern "
                "changing.\n\n"
                + cannot_read
                + " How it works:\n"
                "• Share a person's contact card once — I remember them by a "
                "code, not their number\n"
                "• Forward their messages, or send a chat export for history\n"
                "• I flag pattern *changes* against each pair's own "
                "baseline — and nothing reaches you as an alert without your "
                "review first\n\n"
                "Reply YES to turn this on for you."
            ),
            "buttons": [{"id": "consent:yes", "title": "Turn on OVERLAP"}],
        }
    ]


def _finish_onboarding(owner_id):
    """End the up-to-3-cards onboarding step (Done tap / 'done' / 'skip')."""
    persons = store.persons_for_owner(owner_id)
    state = store.get_agent_state(owner_id)
    store.set_agent_state(owner_id, "idle", state["context"])
    if persons:
        names = ", ".join(p["label"] for p in persons)
        return [
            {
                "text": (
                    f"You're set — watching {names}. Forward their messages "
                    "here, or send a chat export for history and a baseline. "
                    "I'll flag pattern changes against each pair's own "
                    "baseline, and nothing becomes an alert without your "
                    "review first."
                ),
                "buttons": [],
            }
        ]
    return [
        {
            "text": (
                "No problem — share a contact card any time (attach → "
                "Contact), once per person, ever. Then forward their "
                "messages here, or send a chat export for history."
            ),
            "buttons": [],
        }
    ]


# ---------------------------------------------------------------------------
# Consented conversation
# ---------------------------------------------------------------------------


def _consented(owner_id, user, ev):
    kind = ev["kind"]
    if kind == "button":
        return _handle_button(owner_id, ev.get("button_id") or "")
    if kind == "contact":
        return _register_contact(owner_id, ev.get("contact") or {})
    if kind == "document":
        return _handle_export(owner_id, user, ev.get("document_bytes") or b"")

    # text / forwarded
    text = (ev.get("text") or "").strip()
    low = text.lower()
    state = store.get_agent_state(owner_id)
    ctx = state["context"]

    if (
        state["state"] == "onboarding_cards"
        and not ctx.get("pending_review")
        and low in ("done", "skip", "that's all", "that's it", "no more", "skip for now")
    ):
        return _finish_onboarding(owner_id)

    if low == "forget me":
        store.delete_owner_data(owner_id)
        return [{"text": "Done — everything I held for you is deleted.", "buttons": []}]
    if low == "status":
        return [_status(owner_id)]
    if low in ("evidence", "proof"):
        return [_evidence_report(owner_id)]
    if low in ("help", "hi", "hello", "hey"):
        return [
            {
                "text": (
                    "Send me:\n"
                    "• a contact card (attach → Contact) — once per person, ever\n"
                    "• forwarded messages from that person\n"
                    "• a chat export file for their history + baseline\n"
                    "Anything you type to me is just conversation — I only "
                    "log forwards, exports and screenshots.\n"
                    "Say 'status' for what I'm watching, 'forget me' to delete "
                    "your data."
                ),
                "buttons": [],
            }
        ]
    if state["state"] == "awaiting_whoami" and low.startswith("i am "):
        return _resolve_whoami(owner_id, user, text[5:].strip())
    if ctx.get("pending_review") and low in ("confirm", "dismiss"):
        return _apply_review(owner_id, ctx["pending_review"], low)
    if state["state"] == "awaiting_attribution":
        person = _find_person_for_name(owner_id, text)
        if person:
            return _attribute_pending(owner_id, person)
        if low in ("new", "new person", "someone new"):
            store.set_agent_state(owner_id, "awaiting_contact", ctx)
            return [
                {
                    "text": "Share their contact card (attach → Contact) — "
                    "once per person, ever — and I'll log these messages to them.",
                    "buttons": [],
                }
            ]
        if ev["kind"] == "text":
            return [
                {
                    "text": "Still need the name for the messages you "
                    "forwarded — tap a name above or reply with it. (What "
                    "you just typed wasn't logged.)",
                    "buttons": [],
                }
            ]
    if ev["kind"] == "forwarded":
        # A forward ends onboarding implicitly — the user is already
        # using the bot, so don't keep prompting for cards.
        if state["state"] == "onboarding_cards":
            store.set_agent_state(owner_id, "idle", ctx)
        return _submit_message(owner_id, ev)
    # Plain typed text is the user talking to the agent. Read it, answer
    # it — never file it as someone's messages.
    return _conversation(owner_id, ev)


def _conversation(owner_id, ev):
    """Handle plain typed text: conversation only, nothing is logged.

    The one exception is a paste that looks like lines from a chat —
    that gets staged and the user is asked before anything is filed.
    """
    text = (ev.get("text") or "").strip()
    low = text.lower()
    state = store.get_agent_state(owner_id)
    ctx = state["context"]
    if _looks_like_chat(text):
        ctx["pending_paste"] = {
            "text": text,
            "sent_at": ev.get("sent_at") or _now().isoformat(),
            "channel": ev["channel"],
        }
        store.set_agent_state(owner_id, state["state"], ctx)
        return [
            {
                "text": "That looks like messages from a chat, so I "
                "haven't logged it. Want me to log it — or were you just "
                "showing me?",
                "buttons": [
                    {"id": "paste:log", "title": "Log it"},
                    {"id": "paste:talk", "title": "Just talking"},
                ],
            }
        ]
    if low in _ACKS:
        return [
            {
                "text": "Anytime. Forward me their messages whenever — "
                "or say 'status' to see what I'm watching.",
                "buttons": [],
            }
        ]
    if low.endswith("?") or (low.split(" ", 1)[0] if low else "") in _QUESTION_STARTERS:
        return [
            {
                "text": "I watch what you send me: share a person's "
                "contact card once, then forward their messages (or send "
                "a chat export) and I log them under that person. I "
                "compare each pair against its own baseline and flag "
                "pattern *changes* — never verdicts — and nothing becomes "
                "an alert without your review.\n\n"
                "Anything you type to me is just conversation; I never "
                "log your words as someone's messages. Say 'status' to "
                "see what I'm watching.",
                "buttons": [],
            }
        ]
    return [
        {
            "text": "Read you — not logging that. I only log what you "
            "*forward* me, plus chat exports. If those were someone's "
            "messages, forward the originals and I'll file them properly.",
            "buttons": [],
        }
    ]


def _handle_button(owner_id, button_id):
    if button_id == "onboarding:done":
        return _finish_onboarding(owner_id)
    if button_id.startswith("attr:"):
        prefix = button_id[len("attr:"):]
        for person in store.persons_for_owner(owner_id):
            if person["identifier_hash"].startswith(prefix):
                return _attribute_pending(owner_id, person)
        return [{"text": "I don't have that person saved anymore.", "buttons": []}]
    if button_id.startswith("review:"):
        parts = button_id.split(":")
        if len(parts) == 3:
            return _apply_review(owner_id, parts[1], parts[2])
    if button_id == "consent:yes":
        return _pre_consent(owner_id, {"kind": "button", "button_id": "consent:yes", "text": ""})
    if button_id == "paste:log":
        state = store.get_agent_state(owner_id)
        ctx = state["context"]
        paste = ctx.pop("pending_paste", None)
        store.set_agent_state(owner_id, state["state"], ctx)
        if not paste:
            return [
                {
                    "text": "Nothing waiting to log — paste it again and "
                    "tap Log it.",
                    "buttons": [],
                }
            ]
        return _submit_message(
            owner_id,
            {
                "channel": paste.get("channel", "whatsapp"),
                "kind": "forwarded",
                "text": paste["text"],
                "sent_at": paste["sent_at"],
            },
        )
    if button_id == "paste:talk":
        state = store.get_agent_state(owner_id)
        ctx = state["context"]
        ctx.pop("pending_paste", None)
        store.set_agent_state(owner_id, state["state"], ctx)
        return [{"text": "Got it — just between us. Nothing logged.", "buttons": []}]
    return [{"text": "Got it.", "buttons": []}]


# ---------------------------------------------------------------------------
# Once per person, ever
# ---------------------------------------------------------------------------


def _register_contact(owner_id, contact):
    phone = contact.get("wa_id") or contact.get("phone") or ""
    name = (contact.get("name") or "").strip() or "Unknown"
    try:
        ident = normalize_identifier(str(phone))
    except ValueError:
        # Telegram contact cards can arrive without a usable phone
        # number (or with the number hidden); a Telegram user id is
        # still a stable, matchable identifier for that person.
        telegram_id = contact.get("telegram_id")
        if telegram_id is None:
            return [
                {
                    "text": "That contact card didn't include a usable phone number, "
                    "so I couldn't save it.",
                    "buttons": [],
                }
            ]
        try:
            ident = normalize_identifier(f"telegram:{telegram_id}")
        except ValueError:
            return [
                {
                    "text": "That contact card didn't include a usable phone number, "
                    "so I couldn't save it.",
                    "buttons": [],
                }
            ]
    is_new = store.add_person(owner_id, name, ident)
    person = store.person_by_hash(owner_id, ident["hash"])
    if is_new:
        lead = f"Saved {name} ({ident['masked']}) ✓ Once per person, ever — " \
            "I match on a code made from the number; the number itself isn't stored."
    else:
        lead = f"{name} ({ident['masked']}) is already saved — once per person, ever ✓"

    replies = [{"text": lead, "buttons": []}]

    # Onboarding: count cards toward the up-to-3 ask, and prompt for the
    # next one (or Done). Skipped when an export is waiting — that flow
    # takes over, and the export import ends onboarding instead.
    state = store.get_agent_state(owner_id)
    if state["state"] == "onboarding_cards" and not state["context"].get(
        "pending_export"
    ):
        count = len(store.persons_for_owner(owner_id))
        if count >= ONBOARDING_MAX_CARDS:
            store.set_agent_state(owner_id, "idle", state["context"])
            replies.append(
                {
                    "text": (
                        "That's 3 — you're set. Forward their messages here, "
                        "or send a chat export for history and a baseline. "
                        "I'll flag pattern changes against each pair's own "
                        "baseline, and nothing becomes an alert without your "
                        "review first."
                    ),
                    "buttons": [],
                }
            )
        else:
            replies.append(
                {
                    "text": (
                        f"That's {count} of 3. Share another contact card, or "
                        "tap Done — you can add people later any time."
                    ),
                    "buttons": [_DONE_BUTTON],
                }
            )

    # If an export was waiting for this contact, import it now.
    state = store.get_agent_state(owner_id)
    pending_export = state["context"].get("pending_export")
    if pending_export and _find_person_for_name(owner_id, pending_export["partner_name"]):
        if _first_name(pending_export["partner_name"]) == _first_name(name) or \
                store.person_by_label(owner_id, pending_export["partner_name"]):
            user = store.get_chat_user(owner_id)
            replies += _ingest_parsed_export(owner_id, user, pending_export["parsed"], person)
            state["context"].pop("pending_export", None)
            store.set_agent_state(owner_id, "idle", state["context"])

    notice = _match_notice(owner_id, ident)
    if notice:
        replies.append(notice)
    return replies


# ---------------------------------------------------------------------------
# Forwards / pasted messages + attribution
# ---------------------------------------------------------------------------


def _submit_message(owner_id, ev):
    persons = store.persons_for_owner(owner_id)
    if not persons:
        return [
            {
                "text": "First, share the contact card of whoever sent this "
                "(attach → Contact). Once per person, ever — then I can log "
                "their messages.",
                "buttons": [],
            }
        ]
    state = store.get_agent_state(owner_id)
    ctx = state["context"]
    ctx["channel"] = ev["channel"]
    pending = ctx.get("pending", [])
    pending.append(
        {"text": ev.get("text") or "", "sent_at": ev.get("sent_at") or _now().isoformat()}
    )
    ctx["pending"] = pending

    person = None
    last_hash = ctx.get("last_person_hash")
    last_at = ctx.get("last_attr_at")
    if last_hash and last_at:
        try:
            if _now() - datetime.fromisoformat(last_at) < ATTRIBUTION_WINDOW:
                person = store.person_by_hash(owner_id, last_hash)
        except ValueError:
            person = None
    if person is None and len(persons) == 1:
        person = persons[0]

    if person is not None:
        store.set_agent_state(owner_id, "idle", ctx)
        return _attribute_pending(owner_id, person)

    store.set_agent_state(owner_id, "awaiting_attribution", ctx)
    buttons = [
        {"id": f"attr:{p['identifier_hash'][:12]}", "title": p["label"][:20]}
        for p in persons[:3]
    ]
    return [
        {
            "text": "Who sent this? Tap the name — or reply with it.",
            "buttons": buttons,
        }
    ]


def _attribute_pending(owner_id, person):
    state = store.get_agent_state(owner_id)
    ctx = state["context"]
    pending = ctx.get("pending", [])
    ident = _ident_of_person(person)
    channel = ctx.get("channel", "whatsapp")
    count = 0
    for msg in pending:
        # A pending entry may carry its own source (screenshots staged by
        # ingest_screenshot_for_api do); forwards don't, and keep the
        # channel they arrived on — behavior for them is unchanged.
        source = msg.get("source") or channel
        store.add_signal(
            owner_id=owner_id,
            source=source,
            conversation_id=f"{source}:{ident['hash'][:8]}",
            ident=ident,
            message_text=msg["text"],
            sent_at_iso=msg["sent_at"],
        )
        store.add_pair_message(
            owner_id, ident["hash"], "in", msg["text"], msg["sent_at"], source
        )
        count += 1
    ctx["pending"] = []
    ctx["last_person_hash"] = ident["hash"]
    ctx["last_attr_at"] = _now().isoformat()
    store.set_agent_state(owner_id, "idle", ctx)

    replies = [
        {"text": f"✓ Logged {count} message{'s' if count != 1 else ''} for "
                 f"{person['label']}.", "buttons": []}
    ]
    notice = _match_notice(owner_id, ident)
    if notice:
        replies.append(notice)
    _flag_checks_for_others(owner_id, ident)
    return replies


# ---------------------------------------------------------------------------
# Chat exports
# ---------------------------------------------------------------------------


def _handle_export(owner_id, user, data):
    try:
        text = export_parser.extract_export_text(data)
    except Exception:
        return [
            {
                "text": "That file doesn't look like a WhatsApp chat export "
                "(.txt or the .zip WhatsApp gives you).",
                "buttons": [],
            }
        ]
    parsed = export_parser.parse_export(text)
    if not parsed["messages"]:
        return [
            {
                "text": "I couldn't find any messages in that file — is it a "
                "WhatsApp chat export?",
                "buttons": [],
            }
        ]
    return _route_parsed_export(owner_id, user, parsed)


def _route_parsed_export(owner_id, user, parsed):
    owner_name = (user or {}).get("display_name")
    names = parsed["names"]
    if not owner_name or owner_name not in names:
        state = store.get_agent_state(owner_id)
        ctx = state["context"]
        ctx["pending_export_names"] = names
        ctx["pending_export_parsed"] = parsed
        store.set_agent_state(owner_id, "awaiting_whoami", ctx)
        listed = " / ".join(names)
        return [
            {
                "text": f"I found these names in that chat: {listed}. "
                "Which one is you? Reply like: I am <name>",
                "buttons": [],
            }
        ]
    others = [n for n in names if n != owner_name]
    if len(others) != 1:
        return [
            {
                "text": "That export has more than two people in it — group "
                "chats can't be attributed cleanly. Export a 1:1 chat instead.",
                "buttons": [],
            }
        ]
    partner_name = others[0]
    person = _find_person_for_name(owner_id, partner_name)
    if person is None:
        state = store.get_agent_state(owner_id)
        ctx = state["context"]
        ctx["pending_export"] = {
            "partner_name": partner_name,
            "parsed": parsed,
        }
        store.set_agent_state(owner_id, "awaiting_contact_for_export", ctx)
        return [
            {
                "text": f"Found your chat with {partner_name} "
                f"({len(parsed['messages'])} messages). Share "
                f"{_first_name(partner_name).title()}'s contact card once "
                "(attach → Contact) and I'll import this and build the baseline.",
                "buttons": [],
            }
        ]
    return _ingest_parsed_export(owner_id, user, parsed, person)


def _resolve_whoami(owner_id, user, name):
    state = store.get_agent_state(owner_id)
    ctx = state["context"]
    parsed = ctx.get("pending_export_parsed")
    names = ctx.get("pending_export_names", [])
    match = next((n for n in names if n.lower() == name.lower()), None)
    if match is None:
        match = next((n for n in names if _first_name(n) == _first_name(name)), None)
    if match is None or parsed is None:
        return [
            {"text": f"I don't see '{name}' in that chat. Names were: "
                     f"{' / '.join(names)}", "buttons": []}
        ]
    store.set_display_name(owner_id, match)
    ctx.pop("pending_export_parsed", None)
    ctx.pop("pending_export_names", None)
    store.set_agent_state(owner_id, "idle", ctx)
    user = store.get_chat_user(owner_id)
    return _route_parsed_export(owner_id, user, parsed)


def _ingest_parsed_export(owner_id, user, parsed, person):
    ident = _ident_of_person(person)
    owner_name = (user or {}).get("display_name")
    messages = parsed["messages"][:MAX_EXPORT_MESSAGES]
    new_count = 0
    dup_count = 0
    for msg in messages:
        key = export_parser.dedupe_key(msg["sent_at"], msg["sender"], msg["text"])
        if not store.mark_ingested(owner_id, key, "export"):
            dup_count += 1
            continue
        direction = "out" if msg["sender"] == owner_name else "in"
        store.add_pair_message(
            owner_id, ident["hash"], direction, msg["text"], msg["sent_at"], "whatsapp"
        )
        if direction == "in":
            store.add_signal(
                owner_id=owner_id,
                source="whatsapp",
                conversation_id=f"whatsapp:{ident['hash'][:8]}",
                ident=ident,
                message_text=msg["text"],
                sent_at_iso=msg["sent_at"],
            )
        new_count += 1

    replies = [
        {
            "text": f"Imported your chat with {person['label']}: {new_count} new "
            f"messages ({dup_count} already known). Baseline updated.",
            "buttons": [],
        }
    ]

    # Relationship detection over the full pair timeline.
    timeline = [
        {
            "from": person["label"] if row["direction"] == "in" else "you",
            "text": row["message_snippet"],
            "sent_at": row["sent_at"],
        }
        for row in store.pair_messages_for(owner_id, ident["hash"])
    ]
    detection = events_mod.detect_relationship(timeline)
    if detection["detected"]:
        store.set_pair_state(
            owner_id, ident["hash"], detection["state"], detection["since"]
        )
        when = (detection["event"]["at"] or "")[:10]
        if detection["state"] == "together":
            replies.append(
                {
                    "text": f"Noted from the chat itself: a confession on {when} "
                    "and the reply was a yes — together since then. You never "
                    "have to declare it; I'll watch for changes from that date.",
                    "buttons": [],
                }
            )
        elif detection["state"] == "not_together":
            replies.append(
                {
                    "text": f"Noted from the chat itself: a confession on {when}, "
                    "and the reply was a no — so I won't treat this as a "
                    "protected relationship or alert on it.",
                    "buttons": [],
                }
            )
        else:
            replies.append(
                {
                    "text": f"I spotted a confession on {when} but no clear "
                    "reply — noted as unconfirmed. I don't alert on unconfirmed.",
                    "buttons": [],
                }
            )

    notice = _match_notice(owner_id, ident)
    if notice:
        replies.append(notice)
    flag = _flag_check_for(owner_id, person)
    if flag:
        replies.append(flag)
    _flag_checks_for_others(owner_id, ident)
    # A completed card + export is a finished setup — stop prompting.
    _state = store.get_agent_state(owner_id)
    if _state["state"] == "onboarding_cards":
        store.set_agent_state(owner_id, "idle", _state["context"])
    return replies


def ingest_export_for_api(owner_id, parsed, person, owner_name):
    """Shared entry for the token-authenticated export endpoint."""
    user = {"display_name": owner_name}
    return _ingest_parsed_export(owner_id, user, parsed, person)


# ---------------------------------------------------------------------------
# Screenshots (any app, via the iPhone Shortcut's on-device OCR)
# ---------------------------------------------------------------------------


def ingest_screenshot_for_api(owner_id, ocr_text, source_app=None):
    """Shared entry for POST /ingest/screenshot (token-authenticated).

    A confirmed screenshot (the partner's saved name in the chat header)
    files immediately, exactly like an attributed forward: a signal plus
    a pair message per snippet, source 'screenshot', and the usual
    flag checks for other owners.

    An unconfirmed one (OCR garbled the name, or the header shows a
    nickname) is NOT filed on a guess. Its snippets are staged into the
    same pending list forwards use, the agent state becomes
    awaiting_attribution, and — when the owner has a chat presence — an
    outbox notice names the best guess and carries the usual attribution
    buttons, guess first. A tap (or a reply with the right name) files
    them through _attribute_pending, unchanged.
    """
    parsed = screenshot_parser.parse_screenshot(owner_id, ocr_text)
    person = parsed["person"]
    if person is None:
        return {
            "status": "no_person",
            "attribution": "none",
            "person": None,
            "filed": 0,
            "staged": 0,
            "duplicates": parsed["duplicates"],
        }
    ident = _ident_of_person(person)

    if parsed["confirmed"]:
        count = 0
        for msg in parsed["messages"]:
            store.add_signal(
                owner_id=owner_id,
                source="screenshot",
                conversation_id=f"screenshot:{ident['hash'][:8]}",
                ident=ident,
                message_text=msg["text"],
                sent_at_iso=msg["sent_at"],
            )
            store.add_pair_message(
                owner_id, ident["hash"], "in", msg["text"], msg["sent_at"],
                "screenshot",
            )
            count += 1
        _flag_checks_for_others(owner_id, ident)
        return {
            "status": "filed",
            "attribution": "header",
            "person": person,
            "filed": count,
            "staged": 0,
            "duplicates": parsed["duplicates"],
        }

    state = store.get_agent_state(owner_id)
    ctx = state["context"]
    pending = ctx.get("pending", [])
    for msg in parsed["messages"]:
        pending.append(
            {"text": msg["text"], "sent_at": msg["sent_at"], "source": "screenshot"}
        )
    ctx["pending"] = pending
    store.set_agent_state(owner_id, "awaiting_attribution", ctx)

    chat_user = store.get_chat_user(owner_id)
    if chat_user and parsed["messages"]:
        persons = store.persons_for_owner(owner_id)
        ordered = [person] + [
            p for p in persons if p["identifier_hash"] != person["identifier_hash"]
        ]
        buttons = [
            {"id": f"attr:{p['identifier_hash'][:12]}", "title": p["label"][:20]}
            for p in ordered[:3]
        ]
        store.enqueue_outbox(
            owner_id,
            chat_user["channel"],
            chat_user["address"],
            f"📸 Got a screenshot, but the name at the top didn't match "
            f"anyone you've saved. My best guess is {person['label']} — "
            f"tap the right name and I'll file those "
            f"{len(parsed['messages'])} messages there.",
            buttons,
        )
    return {
        "status": "awaiting_confirmation",
        "attribution": "unconfirmed",
        "person": person,
        "filed": 0,
        "staged": len(parsed["messages"]),
        "duplicates": parsed["duplicates"],
    }


# ---------------------------------------------------------------------------
# Matches, flags, reviews
# ---------------------------------------------------------------------------


def _match_notice(owner_id, ident):
    signals = store.signals_for_hash(ident["hash"])
    owners = {s["owner_id"] for s in signals}
    if len(owners) < 2:
        return None
    if not store.add_notice(owner_id, f"match:{ident['hash'][:16]}"):
        return None
    person = store.person_by_hash(owner_id, ident["hash"])
    label = person["label"] if person else ident["masked"]
    # Tell the other chat users too (once each); API-only owners see it in
    # their matches list as before.
    for other in owners - {owner_id}:
        chat_user = store.get_chat_user(other)
        if chat_user and store.add_notice(other, f"match:{ident['hash'][:16]}"):
            store.enqueue_outbox(
                other,
                chat_user["channel"],
                chat_user["address"],
                f"🔔 Potential overlap: {ident['masked']} also appears in "
                f"another OVERLAP user's submissions. {DISCLAIMER}",
                [],
            )
    return {
        "text": f"🔔 Potential overlap: {label} ({ident['masked']}) also "
        f"appears in another OVERLAP user's submissions. {DISCLAIMER}",
        "buttons": [],
    }


def _flag_check_for(owner_id, person):
    """If this owner is 'together' with person, evaluate pattern change."""
    ident = _ident_of_person(person)
    pair = store.get_pair_state(owner_id, ident["hash"])
    if not pair or pair["relationship_state"] != "together" or not pair["since_iso"]:
        return None
    result = flagging.evaluate(owner_id, "", pair["since_iso"], partner_ident=ident)
    flagged = [r for r in result["reports"] if r.get("flagged")]
    if not flagged:
        return None
    if not store.add_notice(owner_id, f"flag:{ident['hash'][:16]}"):
        return None
    reasons = []
    for report in flagged:
        reasons.extend(report["reasons"])
    match_key = ident["hash"][:16]
    state = store.get_agent_state(owner_id)
    ctx = state["context"]
    ctx["pending_review"] = match_key
    store.set_agent_state(owner_id, state["state"], ctx)
    return {
        "text": f"⚠️ Pattern change for {person['label']} since "
        f"{pair['since_iso'][:10]}:\n"
        + "\n".join(f"• {r}" for r in reasons)
        + "\nThis is change against that pair's own baseline — not a "
        "verdict. Review it:",
        "buttons": [
            {"id": f"review:{match_key}:confirmed", "title": "Keep alerts on"},
            {"id": f"review:{match_key}:dismissed", "title": "Dismiss"},
        ],
    }


def _flag_checks_for_others(submitter_owner_id, ident):
    """A new submission can complete a flag for someone protecting a
    relationship with this sender. Notify them via the outbox."""
    signals = store.signals_for_hash(ident["hash"])
    owners = {s["owner_id"] for s in signals} - {submitter_owner_id}
    for other in owners:
        person = store.person_by_hash(other, ident["hash"])
        if person is None:
            continue
        notice = _flag_check_for(other, person)
        if notice:
            chat_user = store.get_chat_user(other)
            if chat_user:
                store.enqueue_outbox(
                    other, chat_user["channel"], chat_user["address"],
                    notice["text"], notice["buttons"],
                )


def _apply_review(owner_id, match_key, decision):
    if decision not in ("confirmed", "dismissed"):
        return [{"text": "I didn't catch that review choice.", "buttons": []}]
    identifier_hash = store.find_hash_by_match_id(f"match_{match_key}")
    if not identifier_hash:
        return [{"text": "That match is no longer there.", "buttons": []}]
    signals = store.signals_for_hash(identifier_hash)
    if not any(s["owner_id"] == owner_id for s in signals):
        return [{"text": "That match isn't one of yours.", "buttons": []}]
    store.set_review(match_key, owner_id, decision)
    state = store.get_agent_state(owner_id)
    ctx = state["context"]
    ctx.pop("pending_review", None)
    store.set_agent_state(owner_id, state["state"], ctx)
    if decision == "confirmed":
        store.add_alert(match_key, owner_id)
        return [
            {
                "text": "Alerts on for this match ✓ The alert is an internal "
                "record — nothing is sent to anyone. "
                + DISCLAIMER,
                "buttons": [],
            }
        ]
    return [
        {"text": "Dismissed — I won't alert on this match.", "buttons": []}
    ]


def _evidence_report(owner_id):
    """'evidence' / 'proof': the tamper-evident state of your streams.

    Every record you submitted is hash-linked to the one before it, so
    the record can't be quietly edited afterwards — that's the answer
    to "this is fake, what did you read?". This reports, per person,
    the chained-entry count, whether the chain still recomputes, and
    whether a public timestamp anchor covers the stream yet.
    """
    persons = store.persons_for_owner(owner_id)
    if not persons:
        return {
            "text": "No evidence yet — I only hold records for people "
            "you've registered. Share a contact card to start one.",
            "buttons": [],
        }
    lines = ["Your evidence — every record is hash-linked, so edits show:"]
    for person in persons:
        result = evidence_mod.verify_chain(owner_id, person["identifier_hash"])
        label = f"{person['label']} ({person['identifier_masked']})"
        if result["entries"] == 0:
            lines.append(f"• {label}: no entries yet")
            continue
        chain = (
            "chain intact ✓"
            if result["intact"]
            else "⚠️ chain check failed — something changed a stored record"
        )
        anchor = (
            "anchored to a public timestamp ✓"
            if evidence_mod.stream_anchor_status(
                owner_id, person["identifier_hash"]
            )
            == "anchored"
            else "timestamp anchor pending"
        )
        lines.append(
            f"• {label}: {result['chained_entries']} chained entries · "
            f"{chain} · {anchor}"
        )
    lines.append(
        "That's how a report answers 'this is fake': the record can't be "
        "edited after the fact without breaking the chain."
    )
    return {"text": "\n".join(lines), "buttons": []}


def _status(owner_id):
    persons = store.persons_for_owner(owner_id)
    if not persons:
        return {
            "text": "I'm not watching anyone for you yet. Share a contact "
            "card (attach → Contact) to start — once per person, ever.",
            "buttons": [],
        }
    lines = ["Watching for you:"]
    for person in persons:
        pair = store.get_pair_state(owner_id, person["identifier_hash"])
        extra = ""
        if pair and pair["relationship_state"]:
            extra = f" — {pair['relationship_state']}"
            if pair["since_iso"]:
                extra += f" since {pair['since_iso'][:10]}"
        owners = {s["owner_id"] for s in store.signals_for_hash(person["identifier_hash"])}
        if len(owners) >= 2:
            extra += " — potential overlap found"
        lines.append(f"• {person['label']} ({person['identifier_masked']}){extra}")
    return {"text": "\n".join(lines), "buttons": []}
