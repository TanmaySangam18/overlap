# OVERLAP on WhatsApp — channel design

Decided 2026-11-17. Channel: OVERLAP's own WhatsApp number on the official
WhatsApp Business Platform (Cloud API). Users interact with OVERLAP in a
normal WhatsApp chat. No unofficial clients, no linked-device bots — those
break Meta's terms and get numbers banned.

**Status (2026-11-17): BUILT end to end** — export parser with dedupe
(`app/export_parser.py`), the agent engine (`app/agent.py`), and the Cloud
API webhook + sender (`app/whatsapp.py`, dry-run until the owner's Meta
credentials are set). Verified by `scripts/simulate_whatsapp.py` playing
the full Sarah/Peter/Luna story through the real webhook, and by the test
suite. Going live needs the owner's Meta app + WhatsApp Business account +
a registered number + public HTTPS hosting (see README "Going live").

## What Meta gives us automatically

- The identity of whoever messages OVERLAP: profile name + number arrive
  with every inbound message (webhook `contacts` object).
- A `forwarded` flag on forwarded messages.
- Contact cards shared into the chat arrive as structured messages
  including the contact's number (`wa_id`).

## What Meta never gives us

- The original sender of a forwarded message, or which chat it came from.
  Forwards are unattributed by design.
- Any access to the user's other chats. Chat exports cannot be triggered,
  scheduled, or pulled by us — the user performs them in the WhatsApp app.

## Onboarding — ask for cards up front (2026-11-17)

A bot cannot request the phone's contacts permission: that permission
screen only exists inside an installed app, and OVERLAP is a chat, not
an app. So the cold-start ask happens in the first chat instead, right
after consent: the bot asks the user to share **up to 3 contact cards**
— the people they want watched first — counting "1 of 3", "2 of 3",
and finishing at 3 or when they tap Done / reply "done" / "skip".
Sharing a card is still once per person, ever, and the user can add
people later any time. This is the legal version of Facebook-style
contact importing: it gets OVERLAP its first people on day one, but
contacts alone are never the signal — only real forwarded/exported
messages are matched and flagged.

## Kept option 1 — "once per person, ever" (forwards)

- First time a person matters, the user forwards a message and shares that
  person's contact card **once**. OVERLAP stores the number hashed +
  masked (same privacy treatment as every identifier) and links it to the
  user's case.
- After that, each forward gets a one-tap attribution prompt from the bot:
  "Who sent this? [Peter] [Someone new]". A burst of forwards in a row is
  attributed to the last-named person until the user switches.
- Registration is per user. The same person registered by two different
  users (same hashed number) is what creates a cross-user match.

## Kept option 2 — chat export (history + baselines)

- The user exports a 1:1 chat (WhatsApp → chat → Export Chat, without
  media) and shares the .txt into the OVERLAP chat.
- The parser ingests every line: timestamp, sender name, text. Sender
  names pair with the once-per-person contact card to anchor a name to a
  number for cross-user matching.
- **Dedupe:** every export contains the full history again. Ingest only
  messages not already stored (key: timestamp + sender + text hash).
- **Cadence:** weekly is enough. Flagging is baseline-vs-change over
  days/weeks using real timestamps, so detection quality does not depend
  on live capture — only alert speed does.
- **Reminders:** OVERLAP can send a scheduled nudge; outside the 24-hour
  service window it must be a pre-approved template message.

## Pipeline after intake

Both routes produce the same internal signals (`source=whatsapp`) and flow
into the existing matching, relationship-event detection, baseline
flagging, and review-gated alerts. Nothing alerts without user review.

## Build order — done

1. ✅ Export parser + dedupe.
2. ✅ Webhook receiver + contact registration + attribution prompts +
   in-chat review. Remaining: the owner's Meta app / WABA setup and
   public hosting to take it live.
