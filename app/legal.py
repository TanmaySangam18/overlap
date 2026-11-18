"""Plain-language Privacy Policy and Terms for OVERLAP.

Served at /privacy and /terms. These pages describe what the code in
this repository actually does — hashed identifiers, 80-character
snippets, consent before storage, 'forget me' deletion, review-gated
alerts — in the same dark, minimal style as the landing page. Keep them
in sync with the code when behavior changes.
"""

_STYLE = """
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
    background:#0a0705; color:#f3efe9; line-height:1.65; padding:48px 22px 72px}
  main{max-width:720px; margin:0 auto}
  a{color:#ffb25e}
  .home{display:inline-block; margin-bottom:34px; font-weight:800; letter-spacing:-.5px;
    font-size:22px; color:#fff; text-decoration:none}
  h1{font-size:34px; letter-spacing:-.8px; margin-bottom:6px}
  .updated{color:rgba(255,255,255,.45); font-size:13.5px; margin-bottom:34px}
  h2{font-size:19px; margin:30px 0 8px}
  p,li{color:rgba(255,255,255,.78); font-size:15.5px}
  ul{margin:6px 0 6px 22px}
  li{margin:4px 0}
  .callout{border:1px solid rgba(255,178,94,.35); background:rgba(255,178,94,.07);
    border-radius:12px; padding:14px 18px; margin:26px 0}
  .callout p{color:#ffe1b8}
  footer{margin-top:44px; padding-top:18px; border-top:1px solid rgba(255,255,255,.1);
    font-size:13px; color:rgba(255,255,255,.4)}
"""


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — OVERLAP</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
<a class="home" href="/">O.</a>
{body}
<footer>OVERLAP is a beta service. <a href="/">Back to the homepage</a> ·
<a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></footer>
</main>
</body>
</html>
"""


_PRIVACY_BODY = """
<h1>Privacy Policy</h1>
<p class="updated">Last updated: November 17, 2026</p>

<p>OVERLAP is an agent that lives in your chats (WhatsApp and Messages) — there is
no OVERLAP app. It notices when the same person appears in conversations that two
different people have chosen to share with it, and when that person's messaging
pattern changes. This page explains, in plain language, exactly what it stores
and what it never does.</p>

<h2>What we store</h2>
<ul>
<li><b>Hashed identifiers.</b> When you register a person (by sharing their contact
card once) or share their messages, their phone number is stored only as a
SHA-256 hash plus a masked display form such as +***7777. Their raw number is
never stored.</li>
<li><b>Message snippets.</b> For messages you send to OVERLAP, we keep at most the
first 80 characters, plus the time the message was sent. We do not keep full
message bodies.</li>
<li><b>Your chat address.</b> Your own WhatsApp or SMS number, so the bot can reply
to you, and the time you consented. Messaging the bot is impossible without it.</li>
<li><b>Your labels and choices.</b> The names you give the people you register, and
your review decisions (confirm or dismiss) on potential matches.</li>
</ul>

<h2>What we never do</h2>
<ul>
<li>We never read a chat you did not send to OVERLAP. We have no access to your
accounts, your phone, or anyone else's.</li>
<li>We never message third parties. A match is shown only to the people who
submitted messages — never to the person the match is about.</li>
<li>We never identify the other user in a match. Match evidence shows activity as
"you" or "another user", never a name or number.</li>
<li>We never decide that anyone cheated, lied, or did anything wrong. OVERLAP
reports pattern changes — frequency, timing, tone — and nothing more.</li>
<li>We never sell your data.</li>
</ul>

<div class="callout"><p>A potential overlap is not proof of a relationship or misconduct.</p></div>

<h2>Your controls</h2>
<ul>
<li><b>Consent first.</b> Nothing is stored about you until you agree in chat.</li>
<li><b>Forget me.</b> Message the bot "forget me" at any time and everything we
hold about you — snippets, registrations, reviews — is deleted.</li>
<li><b>Review gate.</b> No alert is ever created until you have reviewed a
potential match and explicitly confirmed it.</li>
</ul>

<h2>Tamper-evident records</h2>
<p>So that a report can be checked rather than just believed, every submission
you make is linked to the one before it with a SHA-256 hash (a "hash chain"):
changing any stored snippet afterwards breaks the chain, and the break is
detectable. Periodically, a single hash summarizing all current chains is
anchored to a public timestamp service (OpenTimestamps) — <b>only that hash
ever leaves our server</b> for anchoring, never message content, names, or
numbers. Ask the bot for your "evidence" at any time, and anyone holding an
evidence pack can verify it at <a href="/verify">/verify</a>. The chain proves
a record was not edited after we received it; it cannot prove a message a
user chose to submit was not staged before they sent it.</p>

<h2>Who else touches the data</h2>
<p>Messages reach OVERLAP through Meta's WhatsApp platform or your mobile
carrier's SMS network, and the database lives with our hosting provider. Those
are the only other parties involved, and only as transport and storage.</p>

<h2>Retention</h2>
<p>Your submissions are kept until you delete them with "forget me". We may also
purge old message snippets under an internal retention schedule; registrations
and review choices carry no message text.</p>

<h2>Beta</h2>
<p>OVERLAP is a beta service and this policy may change as it develops. The
current version will always be the one on this page.</p>
"""


_TERMS_BODY = """
<h1>Terms of Use</h1>
<p class="updated">Last updated: November 17, 2026</p>

<h2>The service</h2>
<p>OVERLAP is a beta chat agent for WhatsApp and Messages. You share messages with
it — by forwarding, by sending a chat export, or through an automation you set up
on your own phone — and it tells you when the same person appears in submissions
from two different users, and when that person's messaging pattern changes
compared to their own baseline.</p>

<h2>What OVERLAP does not do</h2>
<ul>
<li>It does not read any chat you did not send it, and it cannot access anyone's
account or device.</li>
<li>It does not contact, message, or notify any third party — including the person
a match is about.</li>
<li>It does not prove anything about anyone's behavior or relationships.</li>
</ul>

<div class="callout"><p>A potential overlap is not proof of a relationship or misconduct.</p></div>
<p>A pattern-change report means only that message frequency, timing, or tone
shifted versus a baseline. There are many ordinary explanations. What you do
with a report is your decision.</p>

<h2>Your responsibilities</h2>
<ul>
<li>Only submit messages you are authorized to share.</li>
<li>Register people and share contact cards only where you have a legitimate
reason to do so.</li>
<li>Do not use OVERLAP to harass, stalk, or surveil anyone.</li>
<li>Alerts are created only after your own review and confirmation — you are
responsible for the reviews you confirm.</li>
</ul>

<h2>Availability and changes</h2>
<p>OVERLAP is provided "as is", as a beta, without warranties of any kind. It may
change, break, or be discontinued. You can delete everything OVERLAP holds about
you at any time by messaging the bot "forget me".</p>

<h2>These terms</h2>
<p>We may update these terms as the service develops; the current version will
always be the one on this page. Continued use after an update means you accept
the updated terms.</p>
"""


PRIVACY_HTML = _page("Privacy Policy", _PRIVACY_BODY)
TERMS_HTML = _page("Terms of Use", _TERMS_BODY)
