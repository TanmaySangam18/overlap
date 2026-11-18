# OVERLAP Screenshot — iPhone setup (any app → OVERLAP)

WhatsApp, Instagram, Tinder, Hinge — none of them let anything read
their chats. But every one of them has to *show* the chat on screen, and
the screen is yours. So this capture layer works at the screen: you
screenshot any chat, share the image to a shortcut, and the shortcut
reads the text out of the image **on your phone** and sends OVERLAP
only the text. The image itself never leaves your phone.

One-time setup takes about two minutes.

## Part A — Get your personal token

Same token as the iMessage automation (see SHORTCUT.md). With the
OVERLAP backend running:

```bash
curl -X POST http://127.0.0.1:8000/tokens \
  -H "Content-Type: application/json" \
  -d '{"owner_id": "sarah"}'
```

Keep the returned `ingest_token` private — it works like a password for
submitting as you.

## Part B — Build the "OVERLAP Screenshot" shortcut

1. Open the **Shortcuts** app → **+** to create a new shortcut.
2. Name it **OVERLAP Screenshot**.
3. At the top of the editor, tap the input line ("Receive Any input
   from Share Sheet") and set it to receive **Images** from the
   **Share Sheet**. (In the shortcut's ⓘ settings, "Show in Share
   Sheet" should be on.) If there's no input, the shortcut asks you to
   pick an image when it runs — the Share Sheet is the fast path.
4. Add the action **Extract Text from Image** and set its input to
   **Shortcut Input**. This runs Apple's OCR on your phone.
5. Add the action **Get Contents of URL** and set:
   - **URL:** `https://YOUR-OVERLAP-SERVER/ingest/screenshot`
   - **Method:** POST
   - **Headers:** add `X-Overlap-Token` = your token from Part A
   - **Request Body:** JSON, with two fields:
     - `text` → tap the value and choose the **Extracted Text** from
       the previous action
     - `source_app` → the fixed text `screenshot`
6. Add the action **Show Notification** with the text
   **Sent to OVERLAP**.
7. Save the shortcut.

## Part C — Use it

1. Open any chat — WhatsApp, Instagram, Tinder, anything.
2. Take a screenshot.
3. Tap the screenshot preview (or open it in Photos) → **Share** →
   **OVERLAP Screenshot**.
4. The "Sent to OVERLAP" notification confirms it landed.

## What happens then

OVERLAP reads the name at the top of the chat. If it matches someone
you've saved (you shared their contact card once, as always), the
messages file under them immediately, tagged as screenshot-sourced, and
matching / pattern-watching treat them like any other messages you
send. If the name doesn't match — OCR garbled it, or the chat shows a
nickname — OVERLAP does **not** guess silently: it messages you in your
OVERLAP chat with its best guess and name buttons, and the messages
file only when you tap the right name.

Screenshots that overlap (you shot the same chat twice while scrolling)
are deduped automatically by message text — nothing double-counts.

## Honest limits

- **One screenful per shot.** A screenshot captures what's visible.
  Scroll and shoot again for more history; overlaps are deduped.
- **View-once content can't be screenshotted.** Disappearing photos
  and view-once messages are blocked by the app or vanish on open —
  there is nothing to capture. Forwarding remains the route for those.
- **OCR can garble names** (and occasionally words). A garbled header
  name is exactly what the in-chat confirmation step is for; snippets
  are short evidence fragments, never treated as exact quotes.
- **OCR text has no left/right.** Your own messages visible in the
  shot may be filed as the other person's, and messages are stamped
  with the time you sent the screenshot, not their original times.
- Group chats file under one person (the header is a group name, so
  it's the guess + confirmation path).
- Revoking is entirely on the phone: delete the shortcut, or ask
  OVERLAP to delete the token.
