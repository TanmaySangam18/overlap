# OVERLAP Mac relay — iMessage capture from your own Mac

Apple offers no webhook or API for personal iMessage. Messages can only
leave iMessage through a device signed into the account. The iPhone
Shortcuts automation (`SHORTCUT.md`) does that for incoming messages.
This relay is the Mac half: it reads the Messages database on your own
Mac and posts new messages to OVERLAP — both directions, with real send
times.

## Why this one matters

- **Both directions.** Shortcuts only catches messages you receive.
  The Mac database also holds messages you sent, so the pattern check
  sees the whole conversation, not half of it.
- **Real timestamps.** Screenshot messages are stamped with the time
  you took the shot. Mac-relayed messages carry the time they were
  actually sent, so the late-night reading is trustworthy.

## What you need

- A Mac signed into your iMessage, that stays on while the relay runs.
- The OVERLAP backend running somewhere reachable (same `render.yaml`
  deploy as the rest of the kit).
- Your ingest token — the same one from the Shortcuts setup
  (`POST /tokens` with your owner id). Treat it like a password.

## Setup (5 minutes, on the Mac)

1. Copy the project to the Mac (or just `scripts/mac_relay.py` — it
   needs nothing but Python 3, standard library only).
2. Give your terminal Full Disk Access, or macOS will block the read:
   **System Settings → Privacy & Security → Full Disk Access → + →
   Terminal** (or whichever terminal you run it from). Without this
   the script fails loudly instead of silently sending nothing.
3. One test pass, posting nothing:

```bash
python3 scripts/mac_relay.py --server https://YOUR-OVERLAP-SERVER \
  --token PASTE_YOUR_INGEST_TOKEN --once --dry-run
```

4. Real run, incoming messages only:

```bash
python3 scripts/mac_relay.py --server https://YOUR-OVERLAP-SERVER \
  --token PASTE_YOUR_INGEST_TOKEN --once
```

5. Leave it running, checking every 5 minutes:

```bash
python3 scripts/mac_relay.py --server https://YOUR-OVERLAP-SERVER \
  --token PASTE_YOUR_INGEST_TOKEN --daemon --interval 300
```

To start it automatically at login, a `launchd` plist that runs the
`--daemon` command above works — the script itself is a plain poller,
so any scheduler that keeps it alive is fine.

## Sent messages

By default the relay only sends messages you **received** — the same
view the Shortcuts automation has. Add `--include-sent` to also relay
messages you sent:

- Sent messages are filed under the **partner** of the 1:1 chat,
  because the ingest endpoint records one sender per message and the
  partner is the person being watched. They are tagged
  `imessage-mac-relay-sent`, separate from incoming
  (`imessage-mac-relay`).
- Sent messages in **group chats are skipped** — there is no single
  partner to file them under.
- Honest caveat: the current flagging counts signals per sender hash,
  so sent messages count toward the partner's volume in the pattern
  check. If you want direction-aware flagging (sent vs received
  counted separately), that is a follow-up change to `app/flagging.py`,
  not a relay change.

## How it works

- The live database is **never opened**. Each pass copies
  `~/Library/Messages/chat.db` to a temp file and reads the copy
  read-only, so it can never corrupt Messages or fight it for a lock.
- State is one number in `~/.overlap-mac-relay-state.json`: the highest
  message ROWID already posted. A restart resumes where it left off.
- If a post fails (server down, bad token, rate limit), the pass stops
  there and state stays behind the failed message, so it retries on
  the next pass instead of being silently skipped.
- Only text, the sender's phone/email, and the send time leave the
  Mac. Attachments are never sent. Nothing is posted without the
  token, and the token can be revoked server-side.

## Honest limits

- Text only — photo-only messages have no text to send and are skipped.
- A poller, not a push: with `--daemon --interval 300` a message is at
  most 5 minutes late, never lost.
- The Mac has to stay on. If it sleeps, the relay sleeps with it and
  catches up on the next pass (state is by ROWID, so nothing is
  missed — it just arrives late).
- This reads *your* Mac, *your* account, *your* messages. It cannot
  read anyone else's iMessage, and it is not a way to watch a phone
  you do not own.

## Tests

`tests/test_mac_relay.py` (9 tests) builds a fake `chat.db` and checks
Apple-epoch conversion, incoming/sent/group/empty filtering, the
payload shape, the token header, dry-run state handling, and
stop-and-retry on a failed post. Run the full suite with
`.venv/bin/python -m pytest tests/ -q` — 63 tests pass.
