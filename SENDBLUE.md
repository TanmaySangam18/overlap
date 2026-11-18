# OVERLAP on iMessage — the Sendblue line

## Why this exists

The iMessage-native agents (Series and peers) do not reverse-engineer
Apple and do not run a Mac farm of their own at small scale: they rent
an **iMessage API provider**. The provider runs real Apple devices and
hands the agent its own phone number. Users text that number in
Messages (blue bubbles), the provider POSTs inbound messages to a
webhook, and replies go out over a REST API.

Sendblue is the oldest of these providers (Sendblue / Linq / Blooio /
Project Blue are the market). Its free sandbox needs **no card** and
fits the $0-before-proof rule:

- Free plan: a **shared** line, up to **10 verified contacts**.
- A contact verifies by texting the line once — which is exactly how
  OVERLAP starts anyway (the user texts the bot first).
- Paid AI Agent plan ($100/mo per line, later, post-proof): a
  dedicated number, anyone can text it, 1,000 inbound contacts/day.

## What OVERLAP does with it

`app/sendblue.py` is a transport with the same shape as WhatsApp's:

- Inbound: `POST /webhooks/sendblue` (the provider's receive webhook).
- Outbound: `POST https://api.sendblue.co/api/send-message` with the
  `sb-api-key-id` / `sb-api-secret-key` headers.
- Dry-run until keys are set: sends are logged, nothing leaves.
- Optional webhook check: when `SENDBLUE_WEBHOOK_SECRET` is set,
  deliveries must carry it in the `sb-signing-secret` header.
- Failed sends queue to the outbox and retry on the next delivery —
  no reply is ever silently lost (the WhatsApp lesson).

Channel differences, handled in the transport:

- **No reply buttons in iMessage** → options render as text; the
  engine already accepts typed YES / DONE / a name / CONFIRM / LOG IT.
- **No "forwarded" flag in iMessage** → everything typed is
  conversation; a pasted chat is staged and filed only on "log it".
- **Contact cards** arrive as `.vcf` media attachments → parsed
  (name + first phone), so onboarding works like WhatsApp's.
- Groups and our own outbound echoes are ignored.

## Setup (one time, ~10 minutes, $0)

1. Create the free account (assigns the shared line + API keys):
   - On any machine with Node 18+: `npm install -g @sendblue/cli`,
     then `sendblue setup` (email + a verification code).
   - Or in the dashboard: https://dashboard.sendblue.com
2. Note the assigned number and keys: `sendblue show-keys`,
   `sendblue lines`.
3. In Render → overlap → Environment, set:
   - `SENDBLUE_API_KEY_ID`, `SENDBLUE_API_SECRET_KEY`,
     `SENDBLUE_FROM_NUMBER` (the assigned line, E.164),
     `SENDBLUE_WEBHOOK_SECRET` (pick any long random string).
   Saving redeploys — remember the free instance's DB wipes on deploy.
4. Point the receive webhook at the backend:
   `sendblue webhooks set-receive https://overlap-srd9.onrender.com/webhooks/sendblue`
   (set the same signing secret in the Sendblue dashboard/CLI if the
   CLI asks for one).
5. Test: from the iPhone, text the assigned number "hi" — the bot
   answers in blue bubbles. Add up to 9 more testers the same way
   (`sendblue add-contact +1...`, then they text the line once).

## Honest limits

- Free line is **shared** and capped at 10 verified contacts; the
  dedicated line is a launch cost ($100/mo), post-proof only.
- The provider sees message content in transit (it is a messaging
  provider, SOC 2 Type II). OVERLAP still stores only hashed
  identifiers + 80-char snippets on our side.
- iMessage capture of a user's *own* chats is unchanged: Shortcuts on
  iPhone / Mac relay. Sendblue is the agent's own number — the Series
  shape — not a window into anyone's account.
