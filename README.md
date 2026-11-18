# OVERLAP MVP

A small, privacy-conscious prototype of the OVERLAP matching service.

## What this prototype does
- Accepts message signals that a signed-in user explicitly submits.
- Normalizes sender identifiers (phone numbers / email addresses).
- Looks for the same sender appearing in independently submitted conversations.
- Returns a neutral "potential overlap" result with minimal evidence.
- Requires an explicit user review before any alert is created.
- **Runs as a chat agent, not an app** (`app/agent.py`): users talk to
  OVERLAP inside WhatsApp (official Business number, `app/whatsapp.py`)
  or inside Messages (SMS agent number, `app/sms.py`). One brain, every
  surface: consent onboarding, "once per person, ever" contact-card
  registration, forwarded-message attribution, chat-export ingestion,
  match notices, pattern-change flags, and tap-to-review — all in chat.

## What it does NOT do
- It does not read iMessage or WhatsApp automatically. (Telegram is
  the exception, and only with the owner's own login: the Telethon
  user client reads the owner's own cloud chats after they log in
  once — see `TELEGRAM.md`.)
- It does not access another person's account or device.
- It does not decide that anyone cheated.
- It does not send messages to Sarah, Peter, Bryan, or any third party.
  Alerts in this prototype are internal records only.

Every intake route is platform-supported and user-authorized: the user
sends OVERLAP what it sees (forwards, contact cards, exports), or their
own phone's Shortcuts automation submits for them. A Mac companion remains
a possible future integration on the same terms.

## Privacy choices in this build
- Raw sender identifiers are never stored. Identifiers are canonicalized,
  hashed with SHA-256, and matched on the hash; only a masked form
  (e.g. `+***4567`, `j***@gmail.com`) is kept for display.
- Message text is stored only as an 80-character snippet — the most the API
  ever returns as evidence.
- Match evidence never identifies the other submitting user.
- Every response carries the disclaimer: a potential overlap is not proof of
  a relationship or misconduct.

## Run locally

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for the API explorer.

Storage defaults to a local `overlap.db` SQLite file; set `OVERLAP_DB_PATH`
to change it.

## Run the tests

```bash
python -m pytest -q
```

## Relationship detection and flagging

Users do not declare relationships. OVERLAP detects them from the messages
themselves (`app/events.py`): an explicit confession/proposal phrase is
noted as an event, and the other person's reply decides the state —
acceptance means "together" from that date, rejection means "not_together",
anything else stays "unconfirmed". A bare "ily" is never a confession;
between friends it is often normal.

Flagging (`app/flagging.py`) measures CHANGE against each pair's own
baseline from the relationship start: a flag needs a frequency jump
(>=3x and >=5 messages/week) plus a late-night shift or newly-appearing
affectionate language. Affection that was always there cannot drive a flag.
No baseline history means no flag — change cannot be measured without a
before.

## Test it immediately (scenario simulator)

Weeks of history are compressed into scripted, backdated message timelines:

```bash
python scripts/simulate.py
```

It runs the Sarah/Peter/Luna story three ways: spike after an accepted
confession (flags), unchanged friendship after acceptance (no flag), and a
spike after a rejected confession (no alert path at all).

The full WhatsApp bot story — onboarding, contact card, exports, match
notice, flag, in-chat review — runs through the real webhook with
Cloud API-shaped payloads:

```bash
python scripts/simulate_whatsapp.py
```

The same story on the Telegram bot, through the real webhook with
Bot API-shaped updates (callback-query review included):

```bash
python scripts/simulate_telegram.py
```

## Example

Submit a signal as user `bryan`:

```bash
curl -X POST http://127.0.0.1:8000/signals \
  -H "Content-Type: application/json" \
  -d '{
    "owner_id": "bryan",
    "source": "whatsapp",
    "conversation_id": "chat-1",
    "sender_identifier": "+15551234567",
    "message_text": "I love you ❤️",
    "sent_at": "2026-11-17T14:30:00Z",
    "consent_confirmed": true
  }'
```

Submit a second signal from a separate account using the same sender
identifier (any formatting — `(555) 123-4567` works). The API returns a
potential match. The result is not proof of a relationship or misconduct.

## iPhone automation (iMessage)

A user's iPhone can submit automatically using Apple's own Shortcuts
automation: the phone's "When I receive a message" trigger runs a shortcut
that posts each new message to `POST /ingest/shortcut` with the user's
personal token. Setup is one-time, on the user's own phone, and revocable
there. Full recipe: see `SHORTCUT.md`.

**Mac relay** — the same endpoint, from the user's own Mac:
`scripts/mac_relay.py` reads a copy of `~/Library/Messages/chat.db`
(read-only, never the live file) and posts new messages, both
directions, with their real send times. Incoming-only by default;
`--include-sent` also relays sent messages, filed under the 1:1
partner. Needs Full Disk Access for the terminal running it, and the
Mac stays on while it polls. Full recipe: see `MAC-RELAY.md`.

## The chat agent (no app)

**WhatsApp bot** — OVERLAP's own number on the official WhatsApp Business
Platform (Cloud API). Webhook: `GET/POST /webhooks/whatsapp`. Configure
with env vars `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_ACCESS_TOKEN`,
`WHATSAPP_PHONE_NUMBER_ID`; without the token it runs in dry-run mode
(outbound messages are logged, not sent) so everything is testable.
Design and platform facts: see `WHATSAPP.md`.

**Messages agent** — there is no self-serve iMessage bot API: a
blue-bubble agent requires Apple Messages for Business approval (via an
authorized messaging provider) or an always-on Mac relay. The route that
works today with official APIs is an SMS agent number: on an iPhone the
conversation lives in the Messages app, and iMessage *intake* is covered
by the Shortcuts automation above. Webhook (Twilio-shaped):
`POST /webhooks/sms`, answered with TwiML.

**Telegram bot** — OVERLAP as a BotFather bot, no business account or
verification, token never expires. Webhook: `POST /webhooks/telegram`
(secret-token check when `TELEGRAM_WEBHOOK_SECRET` is set). Same agent
brain: consent, contact cards, forwards, exports, tap-to-review via
inline-keyboard callback queries. Design and platform facts: see
`TELEGRAM.md`.

**Telegram user client (automatic)** — the Tier-1 Telegram shape: a
Telethon client logged into the user's own account reads their own
cloud chats automatically and posts to `POST /ingest/telegram`
(`scripts/telegram_client.py`). Secret chats are invisible to it by
design. Incoming-only by default; `--include-sent` also files sent
messages under the 1:1 partner. A sender whose phone Telegram hides
is filed by Telegram user id (`telegram:<id>`), which matches across
owners — but phone-registered and Telegram-id-registered versions of
the same person do not match each other; registering from the contact
card keeps them in sync.

**WhatsApp chat exports** — `app/export_parser.py` parses iOS and Android
export layouts (date order detected from the data), skips system lines,
and dedupes on re-export so only new messages are ingested. The bot
accepts exports as document messages; the same pipeline is exposed at
`POST /ingest/export` for token holders.

## API summary
- `POST /signals` — submit a signal (`consent_confirmed` must be `true`).
  The response includes `potential_match` when another user has submitted
  the same sender.
- `POST /tokens` — issue a personal ingest token for the Shortcuts
  automation (creating it is the recorded consent grant).
- `POST /ingest/shortcut` — receive one message from a user's own iOS
  Shortcuts automation (header `X-Overlap-Token`); stored as a signal with
  source `imessage`.
- `GET /matches?owner_id=...` — list potential overlaps for your submissions.
- `GET /matches/{match_id}?owner_id=...` — one match, participants only.
- `POST /matches/{match_id}/review` — record your review:
  `{"owner_id": "...", "decision": "confirmed" | "dismissed"}`.
- `POST /matches/{match_id}/alert` — create an internal alert record.
  Blocked (409) until you have confirmed the match in a review.
- `POST /ingest/export` — ingest a WhatsApp chat export (header
  `X-Overlap-Token`); registers the partner and returns the agent's
  summary, detected relationship state, and any potential match.
- `GET/POST /webhooks/whatsapp` — the WhatsApp bot (Cloud API webhook
  verification + inbound messages).
- `POST /webhooks/telegram` — the Telegram bot (Bot API webhook,
  secret-token check when configured, inline-keyboard replies).
- `POST /ingest/telegram` — receive one message from a user's own
  Telegram user client (header `X-Overlap-Token`); stored as a signal
  with source `telegram`.
- `POST /webhooks/sms` — the Messages-app SMS agent (Twilio-shaped
  webhook, TwiML replies).

## Going live (needs the owner's own accounts)
- **WhatsApp:** create a Meta developer app + WhatsApp Business account,
  register the OVERLAP number, set the three `WHATSAPP_*` env vars, host
  this backend at a public HTTPS address, and point the webhook at
  `/webhooks/whatsapp`. Test mode reaches 5 numbers before business
  verification.
- **Messages agent:** a Twilio (or compatible) number with its messaging
  webhook pointed at `/webhooks/sms`.
- **Hosting:** any always-on host with HTTPS; the backend is a single
  FastAPI process + SQLite file for the prototype.

## Important production work still required
- Real authentication and authorization (replace demo `owner_id` values).
- Encrypted database/storage, retention controls, deletion/export workflows.
- Per-source and per-conversation consent records.
- Rate limiting, abuse monitoring, audit logs, and security review.
- A robust sender identity verification strategy; phone numbers can be recycled
  or shared.
- Human review and notification permissions.
- Legal/privacy review before any real-world launch.
