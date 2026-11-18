"""Anchor OVERLAP's evidence chains to a public timestamp calendar.

Computes one root hash over all current per-(user, person) chain tips
and submits it to an OpenTimestamps calendar, which works it into a
Bitcoin-anchored timestamp — after that, the chains provably existed
at that time and cannot have been quietly rewritten since. Only the
32-byte root hash leaves the server: no message content, no
identifiers, no names.

Run it daily in production (host cron / Render Cron Job / Fly
machine), from the repo root:

    python scripts/anchor.py

Behavior:
- Every run first retries anchor records still marked 'pending' (a
  calendar outage or blocked network leaves them pending — the root
  stays recorded locally, and a later run completes the submission).
- A new record is created only when the root has changed since the
  last one; re-running on an unchanged database is a no-op.
- Always exits 0. A calendar failure is a printed status, not a crash:
  anchoring must never take the operator's cron down with it.

Configuration (environment):
- OVERLAP_DB_PATH    the database (default: overlap.db)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import evidence  # noqa: E402


def main() -> int:
    result = evidence.run_anchor()
    if result["root"] is None:
        print("No chained evidence entries yet — nothing to anchor.")
        return 0
    print(f"Evidence root: {result['root']}")
    if not result["attempts"]:
        print("Root unchanged since the last anchor — nothing new to submit.")
    for attempt in result["attempts"]:
        if attempt["status"] == "submitted":
            print(
                f"  anchor #{attempt['id']}: submitted to the timestamp "
                "calendar ✓"
            )
        else:
            print(
                f"  anchor #{attempt['id']}: still pending "
                f"({attempt.get('error') or 'calendar unreachable'}) — the "
                "root is recorded locally and the next run retries it."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
