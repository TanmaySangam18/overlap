"""Baseline-vs-change flagging.

Presence of messages means nothing. A flag requires CHANGE against the
sender's own baseline with a person, measured from the start of the
protected relationship, and several independent changes at once:

- frequency: messages/week at least triples AND reaches 5+/week
- plus at least one of:
  - late-night shift: share of messages between 23:00 and 05:00 rises
    by 25+ percentage points
  - new affection: affectionate language that was basically absent in
    the baseline becomes common afterwards

Affection that was ALWAYS there (the friends' normal "ily") cannot drive a
flag by itself — it only counts when it is new for that pair. If there is
no baseline history, the honest answer is "insufficient baseline" and
nothing flags: change cannot be measured without a before.

A flag is a pattern-change report for a human to judge. It is never a
verdict about anyone's behavior.
"""

from datetime import datetime

from . import store
from .normalize import normalize_identifier

AFFECTION_TERMS = [
    "ily",
    "love you",
    "babe",
    "baby",
    "miss you",
    "❤️",
    "gorgeous",
    "cutie",
    "handsome",
]

MIN_AFTER_PER_WEEK = 5.0
MIN_FREQ_RATIO = 3.0
MIN_LATE_NIGHT_DELTA = 0.25
AFFECTION_BASELINE_MAX = 0.10
AFFECTION_AFTER_MIN = 0.30


def _is_late_night(iso: str) -> bool:
    hour = datetime.fromisoformat(iso).hour
    return hour >= 23 or hour < 5


def _is_affectionate(text: str) -> bool:
    t = (text or "").lower()
    return any(term in t for term in AFFECTION_TERMS)


def _metrics(signals, window_start: datetime, window_end: datetime):
    if not signals:
        return {"count": 0, "per_week": 0.0, "late_share": 0.0, "affection_rate": 0.0}
    days = max(7.0, (window_end - window_start).total_seconds() / 86400.0)
    count = len(signals)
    return {
        "count": count,
        "per_week": count / (days / 7.0),
        "late_share": sum(1 for s in signals if _is_late_night(s["sent_at"])) / count,
        "affection_rate": sum(1 for s in signals if _is_affectionate(s["message_snippet"])) / count,
    }


def evaluate(owner_id: str, partner_identifier: str, since_iso: str, partner_ident=None):
    """Evaluate the partner's other conversations for the protected owner.

    Returns one report per other person who submitted messages from the
    partner, comparing their baseline (before `since_iso`) with the period
    after it. `partner_ident` may carry a precomputed normalize result
    (the chat agent stores only hashes, never the raw identifier).
    """
    partner = partner_ident or normalize_identifier(partner_identifier)
    since = datetime.fromisoformat(since_iso)
    all_signals = store.signals_for_hash(partner["hash"])
    other_owners = sorted({s["owner_id"] for s in all_signals} - {owner_id})

    reports = []
    for other in other_owners:
        msgs = [s for s in all_signals if s["owner_id"] == other]
        base = [s for s in msgs if datetime.fromisoformat(s["sent_at"]) < since]
        after = [s for s in msgs if datetime.fromisoformat(s["sent_at"]) >= since]

        if not base:
            reports.append(
                {
                    "contact_owner": other,
                    "flagged": False,
                    "status": "insufficient_baseline",
                    "reasons": [
                        "No messages from before the relationship start were "
                        "submitted, so there is no baseline to measure change "
                        "against."
                    ],
                }
            )
            continue

        base_start = min(datetime.fromisoformat(s["sent_at"]) for s in base)
        after_end = (
            max(datetime.fromisoformat(s["sent_at"]) for s in after) if after else since
        )
        b = _metrics(base, base_start, since)
        a = _metrics(after, since, after_end)

        freq_ratio = (a["per_week"] / b["per_week"]) if b["per_week"] else 0.0
        late_delta = a["late_share"] - b["late_share"]
        affection_new = (
            b["affection_rate"] <= AFFECTION_BASELINE_MAX
            and a["affection_rate"] >= AFFECTION_AFTER_MIN
        )
        freq_jump = freq_ratio >= MIN_FREQ_RATIO and a["per_week"] >= MIN_AFTER_PER_WEEK

        reasons = []
        if freq_jump:
            reasons.append(
                f"Frequency jumped from {b['per_week']:.1f} to {a['per_week']:.1f} "
                f"messages/week ({freq_ratio:.1f}x)."
            )
        if late_delta >= MIN_LATE_NIGHT_DELTA:
            reasons.append(
                f"Late-night share rose from {b['late_share']:.0%} to "
                f"{a['late_share']:.0%}."
            )
        if affection_new:
            reasons.append(
                f"Affectionate language is new for this pair: {b['affection_rate']:.0%} "
                f"of messages before, {a['affection_rate']:.0%} after."
            )

        flagged = bool(freq_jump and (late_delta >= MIN_LATE_NIGHT_DELTA or affection_new))
        if not flagged:
            if not freq_jump:
                reasons.append(
                    f"No frequency jump: {b['per_week']:.1f} -> {a['per_week']:.1f} "
                    "messages/week."
                )
            elif not reasons:
                reasons.append("Frequency rose, but no timing or affection change.")
        reports.append(
            {
                "contact_owner": other,
                "flagged": flagged,
                "status": "pattern_changed" if flagged else "no_significant_change",
                "baseline": b,
                "after": a,
                "reasons": reasons,
            }
        )
    return {
        "protected_owner": owner_id,
        "partner": {"type": partner["type"], "masked": partner["masked"]},
        "since": since_iso,
        "reports": reports,
        "disclaimer": "A pattern change is not proof of a relationship or misconduct.",
    }
