"""Scenario simulator — test OVERLAP's judgement in seconds, not weeks.

Seeds scripted message histories with backdated timestamps into a throwaway
database, then runs relationship detection and baseline-vs-change flagging.

Three versions of the same story (Sarah, Peter, Luna — all friends):

  A. Peter confesses to Sarah, she accepts, then his messages to Luna spike
     late at night          -> relationship auto-detected, FLAG expected
  B. Same start, but his messages to Luna continue exactly as before
                             -> relationship auto-detected, NO flag expected
  C. Peter confesses, Sarah rejects him, then the spike to Luna happens
                             -> NO relationship, so no alert path at all

Run:  python scripts/simulate.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import store  # noqa: E402
from app.events import detect_relationship  # noqa: E402
from app.flagging import evaluate  # noqa: E402
from app.normalize import normalize_identifier  # noqa: E402

PETER = "+15550001111"
SARAH = "+15550002222"

# Peter -> Luna, September: two friends being normal. Occasional daytime
# messages, and "ily" is already part of how they talk.
BASELINE = [
    ("2026-09-08T14:00:00-05:00", "did you see the game last night"),
    ("2026-09-10T15:30:00-05:00", "ily for covering my shift lol"),
    ("2026-09-15T13:00:00-05:00", "happy birthday!!"),
    ("2026-09-22T16:00:00-05:00", "that class was brutal"),
    ("2026-09-29T14:30:00-05:00", "ily you're the best"),
]

CONFESSION = (
    "2026-10-03T20:15:00-05:00",
    "I need to tell you something. I'm in love with you. Will you be my girlfriend?",
)

# Peter -> Luna after Oct 3, version A/C: near-daily, almost all after 11 PM.
SPIKE = [
    (f"2026-10-{d:02d}T{h}:00-05:00", text)
    for d, h, text in [
        (6, "01:10", "can't sleep. thinking about you"),
        (7, "23:40", "today was long. miss talking to you"),
        (8, "01:05", "you up?"),
        (9, "23:50", "wish you were here"),
        (10, "00:30", "you're honestly my favorite person"),
        (11, "00:15", "still awake?"),
        (12, "01:20", "can't stop thinking about today"),
        (13, "00:40", "just us tomorrow?"),
        (14, "23:55", "you mean a lot to me"),
        (15, "01:40", "miss you"),
        (16, "01:25", "today felt off without you"),
        (17, "01:15", "goodnight ❤️"),
        (19, "00:45", "talk tomorrow? just us"),
        (20, "00:35", "you get me like nobody else"),
        (21, "01:30", "see you in my dreams lol"),
        (22, "23:45", "one more hour?"),
        (24, "00:20", "thinking about you again"),
        (25, "01:45", "wish today never ended"),
        (26, "00:55", "you up?"),
        (27, "01:00", "goodnight, favorite person"),
    ]
]

# Peter -> Luna after Oct 3, version B: unchanged friendship pattern.
CALM = [
    ("2026-10-07T14:00:00-05:00", "that exam was rough"),
    ("2026-10-14T15:00:00-05:00", "ily thanks for the notes"),
    ("2026-10-21T13:30:00-05:00", "see you at sam's on friday"),
]


def seed(owner, sender, text, iso):
    store.add_signal(owner, "imessage", f"chat-{owner}", normalize_identifier(sender), text, iso)


def pair_timeline():
    msgs = []
    for s in store.signals_for_hash(normalize_identifier(PETER)["hash"]):
        if s["owner_id"] == "sarah":
            msgs.append({"from": "peter", "text": s["message_snippet"], "sent_at": s["sent_at"]})
    for s in store.signals_for_hash(normalize_identifier(SARAH)["hash"]):
        if s["owner_id"] == "peter":
            msgs.append({"from": "sarah", "text": s["message_snippet"], "sent_at": s["sent_at"]})
    return sorted(msgs, key=lambda m: m["sent_at"])


def run_scenario(title, after_msgs, reply_text):
    fd, path = tempfile.mkstemp(prefix="overlap-sim-", suffix=".db")
    os.close(fd)
    os.unlink(path)
    os.environ["OVERLAP_DB_PATH"] = path
    try:
        for iso, text in BASELINE:
            seed("luna", PETER, text, iso)
        seed("sarah", PETER, CONFESSION[1], CONFESSION[0])
        seed("peter", SARAH, reply_text, "2026-10-03T20:22:00-05:00")
        for iso, text in after_msgs:
            seed("luna", PETER, text, iso)

        print(f"\n=== {title} ===")
        rel = detect_relationship(pair_timeline())
        if not rel["detected"]:
            print("Relationship: none detected.")
            return
        print(f"Event detected: {rel['event']['by']} confessed on {rel['event']['at']}")
        print(f"  \"{rel['event']['snippet']}\"")
        if rel["reply"]:
            print(f"Reply ({rel['reply']['classification']}): \"{rel['reply']['snippet']}\"")
        print(f"Relationship state: {rel['state']}")

        if rel["state"] != "together":
            print("=> No alert path: there is no relationship to protect.")
            return
        result = evaluate("sarah", PETER, rel["since"])
        for report in result["reports"]:
            verdict = "FLAG — pattern changed" if report["flagged"] else f"no flag ({report['status']})"
            print(f"Peter <-> {report['contact_owner']}: {verdict}")
            for reason in report["reasons"]:
                print(f"   - {reason}")
        print(f"   ({result['disclaimer']})")
    finally:
        os.unlink(path)


if __name__ == "__main__":
    run_scenario(
        "A. Accepted, then Peter's messages to Luna spike late at night",
        SPIKE,
        "Yes!! I love you too ❤️",
    )
    run_scenario(
        "B. Accepted, but Peter and Luna talk exactly as before",
        CALM,
        "Yes!! I love you too ❤️",
    )
    run_scenario(
        "C. Sarah rejects Peter, then the spike to Luna happens anyway",
        SPIKE,
        "I'm sorry, I don't feel the same. Let's just be friends.",
    )
