# OVERLAP on Telegram — the two shapes

Telegram is the one Tier-1 channel the bot shape alone cannot give
you. There are two Telegram routes in this project, and they do
different jobs. Use the bot to test in five minutes; use the client
when you want Telegram to be automatic like iMessage.

## Shape 1 — the bot (test it in ~5 minutes, $0)

OVERLAP as a Telegram bot. No business account, no business
verification, no phone number, no 24-hour token — the BotFather token
does not expire, unlike Meta's temporary WhatsApp token.

### Make the bot

1. In Telegram, open **@BotFather** → `/newbot` → name it `OVERLAP`
   → pick a username, e.g. `overlap_watch_bot` → BotFather gives you
   a token. That token is a password: keep it out of screenshots.
2. In **Render** → your `overlap` service → **Environment**, set:
   - `TELEGRAM_BOT_TOKEN` = the BotFather token
   - `TELEGRAM_WEBHOOK_SECRET` = any secret you invent, e.g.
     `telegram-secret-777` (optional, but set it in prod)
   Save — the service redeploys itself (~1 minute).
3. Point Telegram at the webhook (one command, token in the URL):

   ```bash
   curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://overlap-xxxx.onrender.com/webhooks/telegram&secret_token=telegram-secret-777"
   ```

   Use YOUR Render address from DEPLOY-FREE.md Part 2. Telegram
   answers `{"ok":true}`. (If it fails, open your Render address in
   a browser first to wake the free tier, then retry.)

### Test it

1. In Telegram, open your bot and say `hi` → it introduces itself →
   reply `YES`.
2. Share the person, once: attach (📎) → **Contact** → pick the
   person you both know → send. Once per person, ever.
3. Forward a message: open your real chat with that person →
   long-press a message → **Forward** → your bot. It logs it.
4. Your friend does steps 1–3 with their chat with the same person.
   The moment their forwards land, both of you get the
   🔔 Potential overlap message in Telegram.
5. Send a chat export once (attach the .txt/.zip file in the bot
   chat) and it can detect relationships and pattern changes too —
   exports are optional, not the test.

### Honest limits of the bot

- A bot can only see what a user sends it. It cannot read your
  other Telegram chats — that is the bot shape, and it is why the
  client below exists.
- Telegram forwards *do* preserve the origin in the payload
  (`forward_origin`: a user id when the sender allows it, a display
  name when they hid it). WhatsApp strips it. OVERLAP still
  attributes a forward with one tap on the name, because the match
  key is the person's phone / Telegram id as registered from their
  contact card — the forward payload is kept on the event for a
  future auto-attribution step, not used yet.
- Telegram's own export (Desktop → Export chat history) is JSON/HTML,
  not the WhatsApp .txt format the export parser reads. The bot
  accepts .txt/.zip WhatsApp-format exports; a Telegram JSON export
  gets a plain reply saying so. Forward messages instead, or use the
  client for history-free automatic capture.
- The bot's buttons are Telegram inline keyboards (callback queries),
  max 3 per message — same tap-to-review flow as WhatsApp.

## Shape 2 — the user client (automatic, like iMessage)

This is the Tier-1 shape. A user logs in once with their own Telegram
account and a client reads their own cloud chats automatically,
forever. It uses Telethon, Telegram's sanctioned third-party-client
API — the same API real Telegram apps are built on.

### Set it up (one time, ~10 minutes, $0)

1. Get an `api_id` / `api_hash` at **https://my.telegram.org/apps**
   (log in with your Telegram account → create an app — free).
2. Install the client library (only needed on the machine that runs
   the client, not on Render):

   ```bash
   pip install telethon
   ```

3. Run it, pointing at your OVERLAP server, with the same ingest
   token as the iPhone Shortcuts automation:

   ```bash
   python3 scripts/telegram_client.py --server https://YOUR-OVERLAP-SERVER \
       --token PASTE_YOUR_INGEST_TOKEN \
       --api-id YOUR_API_ID --api-hash YOUR_API_HASH
   ```

   The first run asks for your phone number and the login code
   Telegram sends you, then saves a session file
   (`~/.overlap-telegram.session`) so later runs need no code.

### What it sees, and what it never sees

- Your own cloud chats (the default in Telegram). Incoming messages
  are filed under their real sender; sent messages are opt-in
  (`--include-sent`) and filed under the 1:1 partner, like the Mac
  relay. Group-chat sent messages are skipped.
- **Secret chats are invisible** — they are locked to the phone that
  started them, and no connected client can see them. That is by
  design, not a bug.
- Only text, the sender's phone number (when Telegram shows it) or
  Telegram user id, and the send time leave the machine. Photos and
  video are never sent. The session file is a login to your Telegram
  account: keep it private, revoke it any time in Telegram >
  Settings > Devices.
- A sender whose phone Telegram hides is filed by Telegram user id
  (`telegram:<id>`, shown masked as `tg:***4321`). That id is the
  same in every owner's chats, so matching still works — but a person
  registered by phone in one place and by Telegram id in another will
  not match until both sides use the same identifier. Registering
  the person from their Telegram contact card (which carries the
  phone number) keeps the two in sync. This is stated plainly in the
  code and in the tests; it is not papered over.

### Run it

It is a long-running client, not a webhook: the machine it runs on
stays on while it listens. Stop with Ctrl-C; the session file means
the next run resumes without a new login code. `--dry-run` prints
what would be posted and posts nothing.

## What changed in the code

- `app/telegram.py` — Bot API transport: `POST /webhooks/telegram`,
  dry-run log until `TELEGRAM_BOT_TOKEN` is set, webhook-secret check
  when `TELEGRAM_WEBHOOK_SECRET` is set, inline-keyboard replies,
  contact cards, forwards, .txt/.zip export documents.
- `app/normalize.py` — `telegram:<user id>` is now a matchable
  identifier (SHA-256-hashed, masked `tg:***4321`), alongside phone
  and email.
- `app/agent.py` — the consent intro names Telegram inside a Telegram
  chat instead of WhatsApp; a contact card without a usable phone
  number falls back to its Telegram user id.
- `app/main.py` — `POST /ingest/telegram` (token-authenticated, source
  `telegram`), the endpoint the user client posts to.
- `scripts/telegram_client.py` — the Telethon user client.
- `scripts/simulate_telegram.py` — the full Sarah/Peter/Luna story
  through the real Telegram webhook (match, flag, callback-query
  review, forward, export dedupe).
- `tests/test_telegram.py` — 11 tests. Full suite: 89 passing.

## What is still NOT built

- Telegram JSON export parsing (the bot tells the user plainly).
- Auto-attribution of forwards from `forward_origin` (payload is
  kept; the tap flow is unchanged).
- X DMs and Messenger as OVERLAP channels — the other two Tier-1
  names from the platform census. Telegram was the big unbuilt one;
  those two are next if you want them.
