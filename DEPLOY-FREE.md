# OVERLAP — the $0 live test (share-a-message edition)

Goal: you and a friend forward real WhatsApp messages to the OVERLAP bot
and watch it work — no exports, no cost, no credit card anywhere.

The trick: Meta gives every developer a **free WhatsApp test number**
(a +1 555 sandbox number) meant exactly for this, and Render hosts the
bot **free**. Your only job is creating the two free accounts — the code,
config, and every value to type are already prepared here.

Total time: ~15 minutes of clicking. Do the parts in order.

---

## Part 1 — Put the code on GitHub (5 min)

1. Create a free account at **github.com** (or log in).
2. Top-right **+** → **New repository** → name it `overlap` → **Create repository**.
3. On the repo page, click **uploading an existing file**.
4. Unzip `overlap-mvp.zip`, open the `overlap-mvp` folder, select
   **everything in it** (the `app` folder, `web` folder, `render.yaml`,
   `requirements.txt`, the `.md` files) and drag it all into the upload box.
5. **Commit changes**.

## Part 2 — Host it free on Render (5 min)

1. Create a free account at **render.com** (signing up with GitHub is one click).
2. **New +** → **Blueprint** → connect your `overlap` repo → **Apply**.
   The included `render.yaml` configures everything. When it asks for
   `WHATSAPP_ACCESS_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID`, leave them
   blank for now — they come from Part 3.
3. Wait for the deploy to go **Live**. Your bot's address is shown at the
   top, like `https://overlap-xxxx.onrender.com`. Open it once — you
   should see the OVERLAP page. (First load can take ~1 minute: the free
   tier sleeps when idle and wakes on traffic.)

## Part 3 — Get the free WhatsApp test number (5 min)

1. Go to **developers.facebook.com** → log in with Facebook → if asked,
   register as a developer (free).
2. **My Apps** → **Create App** → choose **Business** → name it `OVERLAP` → Create.
3. In the app dashboard, find **WhatsApp** → **Set up**.
4. Open **WhatsApp → API Setup**. On this page you now have:
   - a **test number** (starts +1 555) — this is OVERLAP's number for the test
   - a **Phone number ID**
   - a **temporary access token** (lasts 24 hours — fine for a test session)
5. On the same page, under **Send and receive messages → To → Manage
   phone number list**: add **your** WhatsApp number and **your friend's**.
   Meta sends each of you a code on WhatsApp — enter it to verify.
   (Max 5 numbers. Only verified numbers can talk to the test bot.)
6. Back in **Render** → your `overlap` service → **Environment**, set:
   - `WHATSAPP_ACCESS_TOKEN` = the temporary token from step 4
   - `WHATSAPP_PHONE_NUMBER_ID` = the Phone number ID from step 4
   Save — the service redeploys itself (~1 minute).
7. Back in the Meta app: **WhatsApp → Configuration** → Webhook → **Edit**:
   - Callback URL: `https://overlap-xxxx.onrender.com/webhooks/whatsapp`
     (use YOUR Render address from Part 2)
   - Verify token: `overlap-verify-777`
   - **Verify and save** — it should accept. (If it fails, open your
     Render address in a browser first to wake the service, then retry.)
   - Then click **Manage** next to Webhook fields and tick **`messages`**.

## Part 4 — The test (the fun part)

Both of you: save the test number as a contact named **OVERLAP**.

1. **Say hi.** Message OVERLAP: `hi` → it introduces itself → reply `YES`.
2. **Share the person, once.** In your chat with OVERLAP: attach (📎) →
   **Contact** → pick the person you both know → send. Once per person, ever.
3. **Forward a message.** Open your real chat with that person →
   long-press one of their messages → **Forward** → **OVERLAP**.
   The bot logs it and tells you. Forward a few.
4. **Your friend does steps 1–3** with *their* chat with the same person.
   The moment their forwards land, **both of you get the 🔔 Potential
   overlap message in WhatsApp** — the same person, in two people's chats.
5. Keep forwarding over days and the bot watches the pattern against the
   baseline; if you also send it a chat export once (attach the file in
   the OVERLAP chat), it can detect relationships and pattern changes too
   — but exports are optional now, not the test.

## Honest limits of the free rig

- **The token dies in 24 hours.** Next test day: Meta app → API Setup →
  copy the fresh temporary token → paste it into Render → Environment.
  Two minutes.
- **Free Render sleeps** after ~15 idle minutes. The first forward after
  a nap can take ~a minute to get its reply (Meta retries delivery).
  That's the free tier, not the product.
- **Test data can reset** if the service redeploys (its disk is
  temporary). Fine for testing; production hosting comes later.
- **The test number is Meta's**, unbranded, and only talks to your 5
  verified numbers. A real launch swaps in your own number + business
  verification — the bot code doesn't change at all.
