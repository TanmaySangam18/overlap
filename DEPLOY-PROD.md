# OVERLAP — production launch runbook

How to take the bot from the $0 test rig (DEPLOY-FREE.md) to a real
launch on your own WhatsApp number. Same code, same repo — what changes
is the hosting (always-on, persistent disk), the number (yours, not
Meta's test number), and the token (permanent, not 24-hour).

Read the warnings before you start: two steps are outside your control
and can take days — **Meta business verification** and, if the number
is currently your personal WhatsApp, **number migration**. Start those
first; do the server work while they run.

---

## 1. Hosting — always-on, with a disk that survives deploys

The database is a single SQLite file. If it lives on an ephemeral
filesystem, every redeploy wipes every user. Production needs (a) an
always-on instance (no sleep — webhook replies must be instant) and
(b) a persistent volume mounted at `/data`. The included `Dockerfile`
is built for exactly that.

### Option A — Render (paid)

1. Render dashboard → your service (or **New + → Web Service**) →
   connect this repo → Runtime: **Docker**.
2. Instance type: **Starter** (or higher) — the free tier sleeps and
   has no persistent disk; both are disqualifying for production.
3. **Disks** → Add Disk: mount path `/data`, size 1 GB to start.
4. Set the environment variables from §2.
5. Health check path: `/healthz`.
6. Deploy. Your address is `https://<service>.onrender.com` (or attach
   a custom domain under **Settings → Custom Domains**).

### Option B — Fly.io

1. `fly launch` in this repo (it detects the Dockerfile). Pick an app
   name and region near your users.
2. `fly volumes create overlap_data --size 1` and mount it in
   `fly.toml`:
   ```toml
   [[mounts]]
     source = "overlap_data"
     destination = "/data"
   ```
3. `fly secrets set` each variable from §2 (secrets, not plain env,
   for the token and app secret).
4. `fly deploy`. Health check: point Fly's HTTP check at `/healthz`.

Either way: one instance only. The rate limiter and the SQLite file
both assume a single process; do not scale out horizontally.

## 2. Environment variables (production)

| Variable | Value |
|---|---|
| `OVERLAP_ENV` | `prod` (turns off /docs + OpenAPI, already set in the Dockerfile) |
| `OVERLAP_DB_PATH` | `/data/overlap.db` (already set in the Dockerfile) |
| `WHATSAPP_VERIFY_TOKEN` | a fresh random string you choose; you will type the same string into Meta's webhook config |
| `WHATSAPP_ACCESS_TOKEN` | the **permanent** system-user token from §3 — never the 24-hour temporary one |
| `WHATSAPP_PHONE_NUMBER_ID` | the phone number ID of **your own** number from §4 |
| `WHATSAPP_APP_SECRET` | App Settings → Basic → **App Secret** in your Meta app. Setting this turns on webhook signature verification — forged deliveries start getting 403s |
| `OVERLAP_RETENTION_DAYS` | optional; `0` (default) keeps snippets forever. See §7 before setting it |
| `OVERLAP_RATE_LIMIT_PER_MIN` | optional; per-sender requests/minute, default `120` |
| `OVERLAP_BACKUP_DIR` | optional; where `scripts/backup_db.py` writes, default `backups/` |

## 3. The permanent access token (system user)

The API Setup page's temporary token dies in 24 hours. The permanent
kind is issued to a *system user* in Business Settings:

1. **business.facebook.com** → Business Settings → **Users → System
   Users** → **Add** → name it `overlap-bot`, role **Admin** → Create.
2. With the system user selected → **Add Assets** → **Apps** → your
   OVERLAP app → enable **Full control** → Save.
3. **Generate New Token** → select your app → set an expiration
   (Never, if offered) → permissions: **`whatsapp_business_messaging`**
   and **`whatsapp_business_management`** → Generate.
4. Copy the token immediately (it is shown once) and set it as
   `WHATSAPP_ACCESS_TOKEN` on the host. Redeploy/restart so the bot
   picks it up.

## 4. Register your own WhatsApp number

Do NOT launch on Meta's +1 555 test number — it is unbranded, capped
at 5 recipients, and not yours.

1. Meta app dashboard → **WhatsApp → API Setup** → **Add phone number**.
2. Business profile: display name (e.g. OVERLAP), category, business
   email, website. The display name goes through a quick review.
3. Enter the number and verify it with the SMS/voice code Meta sends.
4. ⚠️ **The number must not be live on the personal WhatsApp app.** If
   it is, registering it here moves it to the WhatsApp Platform: the
   personal app on that number stops working (chats stay on the phone,
   but the account migrates). The safe pattern is a dedicated number
   (a fresh SIM/eSIM) that has never been your personal WhatsApp.
5. After registration, the API Setup page shows the number's **Phone
   number ID** — set it as `WHATSAPP_PHONE_NUMBER_ID` (it is *not* the
   phone number itself).

## 5. Business verification — start this FIRST

Unverified businesses are capped at low messaging tiers (a few hundred
unique users per 24h) and cannot scale. Verification raises the caps
and is required for a serious launch.

1. Business Settings → **Business Info** → **Business Verification** →
   **Start Verification**.
2. Legal business name, address, phone, website — these must match
   your documents exactly.
3. Upload proof Meta accepts: business license, tax document, bank
   statement, or a utility bill showing the legal name + address.
4. Confirm via the code sent to the business phone/email.
5. ⚠️ **Timeline: usually a few days, sometimes weeks, and Meta can ask
   for more documents.** Nothing about your test launch has to wait for
   it — but public scale does. Submit it before everything else on this
   page so the clock is running.

## 6. Point Meta at production

1. Meta app → **WhatsApp → Configuration** → Webhook → **Edit**:
   - Callback URL: `https://<your-prod-domain>/webhooks/whatsapp`
   - Verify token: the exact `WHATSAPP_VERIFY_TOKEN` from §2
   - **Verify and save** (Meta calls the URL; the bot answers).
2. **Manage** webhook fields → subscribe to **`messages`**.
3. Set `WHATSAPP_APP_SECRET` on the host (§2) if you haven't — from
   this moment, unsigned/forged webhook calls are rejected with 403.
4. Make sure the app itself is switched to **Live** mode (top bar of
   the app dashboard) once your real number is connected.

## 7. Backups and retention (operator jobs — nothing runs itself)

- **Backups:** `python scripts/backup_db.py` snapshots the live DB
  (safe while running) to `OVERLAP_BACKUP_DIR` as
  `overlap-YYYYMMDD-HHMMSS.db`. Run it on a schedule (host cron /
  Render Cron Job / Fly machine) and copy snapshots off the box.
  Take one snapshot before every deploy.
- **Retention:** snippet-bearing rows are kept forever by default.
  To purge old message snippets, set `OVERLAP_RETENTION_DAYS` and run,
  on whatever cadence you choose:
  `python -c "from app import store; print(store.purge_old())"`
  Registrations, reviews, and pair state carry no message text and are
  kept; users can always delete everything themselves with "forget me".

## 8. Launch-day smoke test (two testers, live number)

Run through this against the **production** number before announcing:

- [ ] `GET https://<domain>/healthz` returns `{"status": "ok", "db": "ok"}`
- [ ] Homepage loads; `/privacy` and `/terms` load; footer links work
- [ ] `/docs` returns 404 (prod mode is on)
- [ ] Meta webhook config shows **Verified**; a test message arrives in
      the server logs with no signature errors
- [ ] Tester A messages the number `hi` → consent prompt arrives →
      replies `YES` → bot asks for up to 3 contact cards
- [ ] A shares the shared person's contact card → "Saved …"
- [ ] A forwards one of that person's messages → "✓ Logged 1 message"
- [ ] Tester B does the same (card + forward) → **both** A and B receive
      the 🔔 Potential overlap notice, and neither notice names the other
- [ ] (Optional) A sends a chat export → relationship/baseline replies
      arrive; re-sending the same export reports 0 new messages
- [ ] A flag review: tap **Keep alerts on** → "Alerts on for this match"
- [ ] Throwaway check: a third number onboards, then sends `forget me`
      → bot confirms deletion
- [ ] Take a backup snapshot (§7) and confirm the file exists off-box
- [ ] Evidence: `python scripts/anchor.py` runs clean (§10); download a
      pack from `GET /evidence` and paste it into `/verify` → chain intact
- [ ] Server logs clean for 15 minutes of normal chatting

If a step fails, the usual suspects in order: webhook URL/token
mismatch, temporary token pasted instead of the permanent one, phone
number ID copied wrong (it's the ID, not the number), disk not mounted
at `/data` (data vanished on redeploy), app still in Development mode.

## 9. Rollback

Redeploy the previous image/release. The database lives on the volume
and is untouched by rollbacks; if a bad deploy wrote bad data, stop the
service, restore the latest pre-deploy snapshot over `/data/overlap.db`,
and start it again.

## 10. Evidence anchoring (daily job)

Every stored signal is hash-chained per user + person (see
`app/evidence.py`), so a stored record cannot be quietly edited after
the fact — any change breaks the chain and verification finds the
break. Anchoring adds the public timestamp: one root hash over all
current chain tips is submitted to an OpenTimestamps calendar, which
works it into a Bitcoin-anchored timestamp.

- **Run daily:** `python scripts/anchor.py`, from the repo root, with
  the production `OVERLAP_DB_PATH` (host cron / Render Cron Job / Fly
  machine, same as the backup job in §7). It prints the root and each
  record's status and always exits 0.
- **"Pending" is a normal state, not an error.** A record is `pending`
  when the root is safely recorded in the local `anchor_records` table
  but the calendar has not accepted the submission yet (no network,
  calendar busy). Every later run retries pending records
  automatically — the root is never lost, just untimestamped until a
  run gets through. If records stay pending for days, check the host's
  outbound network.
- **Hashes only.** The submission is the 32-byte root digest. No
  message content, identifier, or name ever leaves the server for
  anchoring.
- **Verification is public.** A user downloads their pack from
  `GET /evidence` (with their ingest token); anyone holding a pack can
  check it at **`/verify`** — the check recomputes the chain from the
  pack alone, no account needed. A pack contains only its owner's own
  submissions; a match contributes dates and counts, never the other
  side's content.
