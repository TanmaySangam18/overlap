"""Tamper-evident evidence: hash-chained signals + public timestamping.

Why this exists: when OVERLAP shows a user a pattern report, the other
person's first move can be "this is fake — you edited that." The chain
makes quiet editing detectable:

- Every signal a user submits is hash-linked to the previous signal in
  the same (owner, person) stream: entry_hash = SHA-256 over prev_hash,
  owner, person hash, source, snippet, and received_at (see
  compute_entry_hash for the exact canonical form). The chaining happens
  inside store.add_signal — the one funnel every ingest path uses — so
  forwards, Shortcuts, screenshots, and exports are all chained the
  same way. Editing, deleting, or reordering a chained entry breaks
  the link, and verify_chain finds the first break.
- Periodically (scripts/anchor.py, run daily in production) a single
  root hash over all current chain tips is submitted to a public
  OpenTimestamps calendar, which works it into a Bitcoin-anchored
  timestamp. Only the root hash ever leaves the server: no snippets,
  no identifiers, no content.

Honest limits, by design:

- Rows written before chaining existed carry NULL chain fields. They
  are the chain's "legacy" segment: counted and listed in evidence
  packs, but not hash-verified — verification starts at the first
  chained row of a stream.
- The chain proves OVERLAP's stored record was not altered after the
  fact. It cannot prove that a message a user chose to submit (a
  forward, a screenshot) was not staged before submission — which is
  why evidence packs label each source's strength instead of hiding it.
"""

import hashlib
import urllib.error
import urllib.request

from . import store
from .legal import _page

GENESIS = "GENESIS"
OTS_CALENDAR_URL = "https://alice.btc.calendar.opentimestamps.org/submit"

DISCLAIMER = "A potential overlap is not proof of a relationship or misconduct."

SOURCE_LABELS = {
    "imessage": "automatic capture",
    "export": "export file",
    "screenshot": "screenshot",
}
# Anything else (the chat channels "whatsapp" / "sms") reached OVERLAP
# because the user picked the message and forwarded it to the bot.
DEFAULT_SOURCE_LABEL = "forward"

SOURCE_NOTES = {
    "automatic capture": "Strongest: your own phone's automation sent it as it arrived.",
    "export file": "Strong: the messaging app's own history file, submitted whole.",
    "forward": "You picked each message and sent it to OVERLAP yourself.",
    "screenshot": "OCR of your screen: the text is what your phone read from the image, and can garble.",
}


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


def compute_entry_hash(prev_hash, owner_id, person_hash, source, snippet, received_at):
    """SHA-256 over the canonical entry string.

    Canonical form: the six fields in order (prev_hash, owner_id,
    person_hash, source, snippet, received_at), each length-prefixed
    ("<len>:<value>") and joined with "|", UTF-8 encoded. Length
    prefixes keep field boundaries unambiguous even when a snippet
    contains separator characters.
    """
    fields = [prev_hash, owner_id, person_hash, source, snippet, received_at]
    canonical = "|".join(f"{len(field)}:{field}" for field in fields)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_chain(owner_id, person_hash):
    """Recompute one stream's chain from its stored fields.

    Legacy rows (chain_hash NULL, written before chaining existed) are
    counted in 'entries' but skipped by the check — verification starts
    at the first chained row, whose prev_hash must be "GENESIS".
    """
    rows = store.signals_for_stream(owner_id, person_hash)
    chained = [r for r in rows if r["chain_hash"]]
    intact = True
    first_break = None
    expected_prev = GENESIS
    for row in chained:
        recomputed = compute_entry_hash(
            row["prev_hash"] or "",
            owner_id,
            person_hash,
            row["source"],
            row["message_snippet"],
            row["received_at"],
        )
        if row["prev_hash"] != expected_prev or recomputed != row["chain_hash"]:
            intact = False
            first_break = row["id"]
            break
        expected_prev = row["chain_hash"]
    return {
        "entries": len(rows),
        "chained_entries": len(chained),
        "intact": intact,
        "first_break": first_break,
    }


# ---------------------------------------------------------------------------
# External timestamp anchoring (OpenTimestamps)
# ---------------------------------------------------------------------------


def anchor_root():
    """One hash over every current chain tip (sorted, then SHA-256).

    Returns None when nothing is chained yet — there is nothing to
    anchor. The root commits to the tips only; no content is in it.
    """
    tips = store.all_chain_tips()
    if not tips:
        return None
    return hashlib.sha256("".join(sorted(tips)).encode("utf-8")).hexdigest()


def submit_to_calendar(root_hash, timeout=15):
    """POST one root digest to an OpenTimestamps calendar server.

    The OTS protocol is deliberately tiny: the request body is the raw
    32-byte digest and the response body is the serialized timestamp
    proof. Returns the proof bytes. Raises on any network or HTTP
    failure — callers treat that as "retry on a later run", never as
    data loss. This is the only network call in the evidence layer, so
    tests can mock this one function and never touch the network.
    """
    digest = bytes.fromhex(root_hash)
    request = urllib.request.Request(
        OTS_CALENDAR_URL,
        data=digest,
        method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            "Accept": "application/octet-stream",
            "User-Agent": "overlap-evidence/0.2",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise urllib.error.URLError(
                f"calendar returned HTTP {response.status}"
            )
        return response.read()


def _attempt_submit(record, submit_fn):
    try:
        proof = submit_fn(record["root_hash"])
    except Exception as exc:  # offline, calendar busy — a later run retries
        return {
            "id": record["id"],
            "root_hash": record["root_hash"],
            "status": "pending",
            "error": str(exc),
        }
    store.update_anchor_record(record["id"], "submitted", proof.hex())
    return {"id": record["id"], "root_hash": record["root_hash"], "status": "submitted"}


def run_anchor(submit_fn=None):
    """Record the current root and try to timestamp it; retry pendings.

    Safe to run any time (scripts/anchor.py runs it daily): pending
    records from earlier runs are retried first, and a new record is
    created only when the current root differs from the latest one.
    A failed submission leaves the record 'pending' with the root
    stored locally — never lost, just untimestamped until a run gets
    through.
    """
    submit_fn = submit_fn or submit_to_calendar
    attempts = [_attempt_submit(r, submit_fn) for r in store.pending_anchor_records()]
    root = anchor_root()
    created = None
    if root is not None:
        latest = store.latest_anchor_record()
        if latest is None or latest["root_hash"] != root:
            record = {"id": store.add_anchor_record(root), "root_hash": root}
            created = _attempt_submit(record, submit_fn)
            attempts.append(created)
    return {"root": root, "created": created, "attempts": attempts}


def stream_anchor_status(owner_id, person_hash):
    """'anchored' once a submitted anchor postdates the stream's tip.

    An anchor's root covers every tip that existed when it was taken,
    so a stream is anchored when some submitted anchor was created at
    or after its current tip was received. Anything else is 'pending'.
    """
    chained = [
        r for r in store.signals_for_stream(owner_id, person_hash) if r["chain_hash"]
    ]
    if not chained:
        return "pending"
    tip_received_at = chained[-1]["received_at"]
    for record in store.anchor_records():
        if record["status"] == "submitted" and record["created_at"] >= tip_received_at:
            return "anchored"
    return "pending"


# ---------------------------------------------------------------------------
# Evidence packs
# ---------------------------------------------------------------------------


def build_evidence_pack(owner_id, person):
    """Assemble one user's downloadable evidence pack for one person.

    Contains ONLY the caller's own submissions about that person. A
    match, if one exists, contributes dates and counts — never the
    other owner's snippets or anything identifying them.
    """
    person_hash = person["identifier_hash"]
    rows = store.signals_for_stream(owner_id, person_hash)
    entries = [
        {
            "id": r["id"],
            "received_at": r["received_at"],
            "sent_at": r["sent_at"],
            "source": r["source"],
            "snippet": r["message_snippet"],
            "prev_hash": r["prev_hash"],
            "chain_hash": r["chain_hash"],
        }
        for r in rows
    ]

    # Anchors that could cover this stream: taken at or after its first
    # chained entry arrived (an anchor's root covers the tips of its
    # moment; earlier anchors predate the stream entirely).
    first_chained_at = next(
        (r["received_at"] for r in rows if r["chain_hash"]), None
    )
    anchors = []
    if first_chained_at:
        anchors = [
            {
                "root_hash": a["root_hash"],
                "created_at": a["created_at"],
                "status": a["status"],
            }
            for a in store.anchor_records()
            if a["created_at"] >= first_chained_at
        ]

    counts = {}
    for r in rows:
        counts[r["source"]] = counts.get(r["source"], 0) + 1
    source_strength = []
    for source, count in sorted(counts.items()):
        label = SOURCE_LABELS.get(source, DEFAULT_SOURCE_LABEL)
        source_strength.append(
            {
                "source": source,
                "label": label,
                "count": count,
                "note": SOURCE_NOTES[label],
            }
        )

    all_signals = store.signals_for_hash(person_hash)
    owners = {s["owner_id"] for s in all_signals}
    if len(owners) >= 2:
        match = {
            "exists": True,
            "distinct_owner_count": len(owners),
            "signal_count": len(all_signals),
            "first_seen": min(s["sent_at"] for s in all_signals),
            "last_seen": max(s["sent_at"] for s in all_signals),
        }
    else:
        match = {"exists": False}

    return {
        "kind": "overlap_evidence_pack",
        "pack_version": 1,
        "owner_id": owner_id,
        "person": {
            "label": person["label"],
            "type": person["identifier_type"],
            "masked": person["identifier_masked"],
        },
        "person_hash": person_hash,
        "entries": entries,
        "chain_verification": verify_chain(owner_id, person_hash),
        "anchors": anchors,
        "source_strength": source_strength,
        "match": match,
        "disclaimer": DISCLAIMER
        + " This pack proves the records above were not altered after "
        "OVERLAP received them; it cannot prove a submitted message was "
        "not staged before submission.",
    }


def verify_pack(pack):
    """Recompute a pack's chain from the pack alone — no DB, no auth.

    Anyone holding a pack can check it: the hashes either recompute or
    they don't. Legacy entries (chain_hash null) predate chaining and
    are skipped, so 'entries_checked' counts chained entries only.
    anchor_status is read from the pack's own anchor records:
    'anchored' if any was submitted to the calendar, 'pending' if
    records exist but none got through, 'none' if the pack has none.

    HONESTY RULE: that status is SELF-REPORTED by the pack. This
    function recomputes the hash chain (that part is proven), but it
    cannot verify an OpenTimestamps submission — the proof bytes are
    not checked here — so anchor_verified is always False and no
    surface may present the anchor as proven on this function's word.
    """
    try:
        entries = pack["entries"]
        owner_id = pack["owner_id"]
        person_hash = pack["person_hash"]
        if not isinstance(entries, list):
            raise ValueError("entries must be a list")
    except (KeyError, TypeError, ValueError, AttributeError):
        return {
            "chain_intact": False,
            "entries_checked": 0,
            "anchor_status": "none",
            "anchor_verified": False,
        }

    if entries and all(isinstance(e.get("id"), int) for e in entries):
        entries = sorted(entries, key=lambda e: e["id"])

    expected_prev = GENESIS
    checked = 0
    intact = True
    for entry in entries:
        if not entry.get("chain_hash"):
            continue  # legacy segment: listed, not hash-verified
        checked += 1
        recomputed = compute_entry_hash(
            entry.get("prev_hash") or "",
            owner_id,
            person_hash,
            entry.get("source") or "",
            entry.get("snippet") or "",
            entry.get("received_at") or "",
        )
        if entry.get("prev_hash") != expected_prev or recomputed != entry["chain_hash"]:
            intact = False
            break
        expected_prev = entry["chain_hash"]

    anchors = pack.get("anchors") or []
    if any(a.get("status") == "submitted" for a in anchors):
        anchor_status = "anchored"
    elif anchors:
        anchor_status = "pending"
    else:
        anchor_status = "none"
    return {
        "chain_intact": intact,
        "entries_checked": checked,
        "anchor_status": anchor_status,
        "anchor_verified": False,
    }


# ---------------------------------------------------------------------------
# Public /verify page
# ---------------------------------------------------------------------------

_VERIFY_BODY = """
<h1>Verify an evidence pack</h1>
<p class="updated">Public check — no account needed</p>

<p>An OVERLAP evidence pack is a small file a user can download for one
person they registered. Every record in it is hash-linked to the one
before it, so any edit made after OVERLAP received the record breaks
the chain. Paste a pack below and this page recomputes every hash —
nothing you paste is stored.</p>

<textarea id="pack" placeholder="Paste an evidence pack (JSON) here…"
  style="width:100%;min-height:190px;margin:22px 0 12px;padding:14px;
  border-radius:12px;border:1px solid rgba(255,255,255,.2);
  background:rgba(255,255,255,.05);color:#f3efe9;font-size:13.5px;
  font-family:ui-monospace,Menlo,Consolas,monospace"></textarea>

<button id="go" style="background:#ffb25e;color:#1a120a;border:0;
  border-radius:999px;padding:12px 26px;font-size:15.5px;font-weight:700;
  cursor:pointer">Check this pack</button>

<div id="result" style="margin-top:26px"></div>

<div class="callout"><p>A valid chain proves the records were not edited
after OVERLAP received them. It does not prove a message wasn't staged
before it was submitted — and a potential overlap is not proof of a
relationship or misconduct.</p></div>

<script>
const box = document.getElementById('pack');
const out = document.getElementById('result');
document.getElementById('go').addEventListener('click', async () => {
  out.innerHTML = '<p>Checking…</p>';
  let pack;
  try { pack = JSON.parse(box.value); }
  catch (e) {
    out.innerHTML = '<p>✗ That is not valid JSON — an evidence pack is a JSON file.</p>';
    return;
  }
  let r;
  try {
    const resp = await fetch('/evidence/verify', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(pack)
    });
    r = await resp.json();
  } catch (e) {
    out.innerHTML = '<p>✗ The check could not run — try again.</p>';
    return;
  }
  let lines = [];
  if (r.entries_checked === 0) {
    lines.push('This pack has no hash-chained entries to check — its records predate the evidence chain.');
  } else if (r.chain_intact) {
    lines.push('✓ <b>Chain intact.</b> ' + r.entries_checked + ' entries recomputed, and every hash matches. These records were not edited after OVERLAP received them.');
  } else {
    lines.push('✗ <b>This pack does not verify.</b> At least one entry was changed after the fact (' + r.entries_checked + ' entries checked, up to the break).');
  }
  if (r.anchor_status === 'anchored') {
    lines.push('Timestamp anchor: this pack <b>reports</b> a submission to a public timestamp calendar. This page cannot independently verify that submission — what is proven here is the hash chain above.');
  } else if (r.anchor_status === 'pending') {
    lines.push('Timestamp anchor: recorded in the pack, submission to the public calendar still pending.');
  } else {
    lines.push('Timestamp anchor: none in this pack yet.');
  }
  out.innerHTML = lines.map(l => '<p>' + l + '</p>').join('');
});
</script>
"""

VERIFY_PAGE_HTML = _page("Verify evidence", _VERIFY_BODY)
