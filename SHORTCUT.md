# OVERLAP Capture — iPhone setup (iMessage automation)

How a user's iPhone sends new iMessages to OVERLAP automatically, using only
Apple's built-in Shortcuts automation. The user sets this up once, on their
own phone, and can revoke it with one toggle. OVERLAP never reads the phone;
the phone's own system hands each new message to OVERLAP because the user
told it to.

One-time setup takes about two minutes.

## Part A — Get your personal token

With the OVERLAP backend running, create a token for your user id:

```bash
curl -X POST http://127.0.0.1:8000/tokens \
  -H "Content-Type: application/json" \
  -d '{"owner_id": "sarah"}'
```

Keep the returned `ingest_token` private — it works like a password for
submitting as you. (In the real app, the app shows this token after sign-in.)

Note: for the automation to reach the backend away from home, the backend
needs a public HTTPS address. On the same Wi-Fi, a local address works for
testing.

## Part B — Build the "OVERLAP Capture" shortcut

1. Open the **Shortcuts** app → **+** to create a new shortcut.
2. Name it **OVERLAP Capture**.
3. Add the action **Get Contents of URL** and set:
   - **URL:** `https://YOUR-OVERLAP-SERVER/ingest/shortcut`
   - **Method:** POST
   - **Headers:** add `X-Overlap-Token` = your token from Part A
   - **Request Body:** JSON, with two fields:
     - `sender_identifier` → tap the value, choose **Shortcut Input**, then
       pick the input's **sender (phone number / email)** detail
     - `message_text` → tap the value, choose **Shortcut Input**, then pick
       the input's **content** detail
4. Save the shortcut.

When the automation (Part C) runs this shortcut, the incoming message is the
Shortcut Input — that's where the sender and text come from.

## Part C — Create the automation

1. In Shortcuts, go to the **Automation** tab → **+** → **Create Personal
   Automation**.
2. Choose the **Message** trigger ("When I receive a message").
3. **Sender:** pick who this watches — one specific person, or any sender.
   This choice is the user's privacy control.
4. Choose **Run Immediately** (iOS 17 and later; on older iOS versions,
   choose "Run After Confirmation" — each message then needs one tap).
5. Select the **OVERLAP Capture** shortcut from Part B. Done.

## What happens then

Every new iMessage (or SMS) from the chosen senders is posted to OVERLAP the
moment it arrives — sender + text — and turned into a signal with source
`imessage`. Matching runs as before: if the same sender appears in another
user's submissions, a potential overlap exists, and nothing is alerted until
a user reviews and confirms it.

## Honest limits

- Text only: photo/video attachments in a message are not captured.
- Group chats can report the group instead of the individual sender.
- The phone must be able to reach the OVERLAP server (public HTTPS for
  real-world use).
- "Run Immediately" wording varies by iOS version; on some versions the
  automation asks for a tap per message instead.
- Revoking is entirely on the phone: delete the automation, or ask OVERLAP
  to delete the token.
