# OVERLAP on iMessage — in-house, via BlueBubbles on your Mac

No rentals. The iMessage line runs on hardware we own: the free,
open-source **BlueBubbles Server** on your Mac, signed into an Apple
ID. It exposes iMessage over a REST API + webhooks, and OVERLAP's
backend (`app/bluebubbles.py`) speaks exactly that:

- The Mac POSTs every new message to
  `https://overlap-srd9.onrender.com/webhooks/bluebubbles`.
- OVERLAP replies by POSTing to the Mac's `/api/v1/message/text`;
  Messages sends them as real iMessages (blue bubbles).

Which Apple ID decides what the line *is*:

- **A dedicated Apple ID for OVERLAP** → the bot is its own contact,
  the Series shape, with the "device farm" being one Mac we own.
- **Your personal Apple ID** → it's your own Messages, automated
  (same code; the Shortcuts/Mac-relay capture covers this side too).

## Setup (one time, ~15 minutes on the Mac, $0)

1. **Install BlueBubbles Server** on the Mac from bluebubbles.app and
   open it. Sign into **Messages** on that Mac with the Apple ID the
   line should use.
2. Grant **Full Disk Access** when it asks (it reads the Messages
   database — on your Mac, under your account, nowhere else).
3. In BlueBubbles settings: set a **server password** (you pick it;
   it guards the API).
4. Add a **webhook**: URL
   `https://overlap-srd9.onrender.com/webhooks/bluebubbles?key=<WEBHOOK_SECRET>`
   for the **new-message** event. Pick any long random string as
   `<WEBHOOK_SECRET>`.
5. Make the Mac reachable for *sending*: BlueBubbles needs a public
   URL for its API. Use its built-in dynamic-DNS/proxy option, or a
   free Cloudflare Tunnel / zrok pointing at `http://localhost:1234`.
   Note the resulting base URL.
6. In Render → overlap → Environment, set:
   - `BLUEBUBBLES_SERVER_URL` = that base URL
   - `BLUEBUBBLES_PASSWORD` = the server password from step 3
   - `BLUEBUBBLES_WEBHOOK_SECRET` = the same `<WEBHOOK_SECRET>`
   Saving redeploys — remember the free instance's DB wipes on deploy.
7. Test: from the iPhone (a *different* Apple ID/number), text the
   line's number or email "hi" — the bot answers in blue bubbles.

## Honest limits

- The Mac must stay **on and awake** for the line to answer; a sleeping
  Mac = a silent bot (same physics as the Render free tier, one layer
  down). A cheap always-on Mac mini is the classic fix — later,
  post-proof.
- One Mac + one Apple ID = one line. Scale = more Macs. That is
  literally what the rental providers do; we just own the Mac.
- BlueBubbles is unofficial (it drives your own Messages app on your
  own Mac with your own account). Apple has never offered a personal
  iMessage API; this is the strongest self-hosted bridge there is
  (Apache-2.0, actively maintained), and macOS updates occasionally
  require a BlueBubbles update to match.
- OVERLAP still stores only hashed identifiers + 80-char snippets;
  message content transits only between the user's phone, your Mac,
  and the backend.
