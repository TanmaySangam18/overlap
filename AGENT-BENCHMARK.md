# Agent benchmark — what the best chat agents do, and what OVERLAP took

Checked 2026-11-18: Series (iMessage social agent), Yuki/eluv
(iMessage companion — "texts first, infinite memory, one memory across
channels"), Yuki HQ (assistant — approval cards for consequential
actions, an activity log, undo for 30 days), Poke, Pally, Kiwi, Para
(WhatsApp-native, "only sees what you explicitly share").

## Patterns adopted in this pass

1. **Loose phrasing, not exact strings.** The leaders understand
   people; OVERLAP matched commands literally, so "Thanks!" fell
   through. All commands now match on normalized text (case,
   punctuation, emoji-insensitive): thanks/status/help/evidence/
   forget, plus natural forms — "who are you watching?", "delete my
   data", "thank you so much".
2. **Activity log (Yuki).** Say `activity`: your latest stored
   entries, newest first. An agent that holds your data must be able
   to show it back.
3. **Undo (Yuki).** Say `undo` (or `undo <name>`): removes the newest
   logged message. Misattribution is the top accuracy risk in
   OVERLAP; un-logging is the fix. Only the evidence chain's tip is
   ever removed, so the remaining chain still verifies.
4. **Pause / resume.** Say `pause`: forwards and exports are refused,
   nothing is stored, until `resume`. Control without deletion.
5. **Never silent.** Photos, voice notes, videos and stickers used to
   get no reply at all. They now get one line of guidance.
6. **Status that remembers.** `status` now shows per-person logged
   counts and last activity, not just names.

## Reliability corrections underneath

- Webhook dedupe is released when processing fails, so Meta's retry
  is processed instead of swallowed as a duplicate (lost messages).
- Outbox notices are marked sent only *after* a successful send
  (peek + mark); a failed send stays queued. Direct replies that fail
  to send are queued the same way. Match/flag notices can no longer
  vanish silently.
- One WhatsApp message carrying several contact cards registers every
  card (dedupe no longer swallows cards 2+).
- Long replies are chunked to WhatsApp's 4096-char limit instead of
  erroring.
- SMS renders paste decisions and the onboarding Done as typed
  replies (`LOG IT` / `JUST TALKING` / `DONE`), and typed paste
  decisions work on every channel — SMS has no buttons.
- The pre-consent explainer no longer says "WhatsApp" inside an SMS
  conversation.

## Deliberately NOT copied

- **One memory across channels (eluv/Yuki).** OVERLAP's owner id is
  channel-scoped, and the evidence chain hashes the owner id into
  every entry — merging identities across channels would break the
  chain. Cross-channel linking needs its own design; not smuggled in.
- **Texts-first proactivity (eluv/Kiwi).** OVERLAP already texts
  first for the only events that matter to it — matches and pattern
  flags — via the outbox. No chit-chat proactivity; it's a watchdog,
  not a companion.
- **Approval cards (Yuki Agent).** Already OVERLAP's core rule:
  nothing becomes an alert without the user's explicit review tap.
