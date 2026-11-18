"""In-process rate limiting for the webhook and ingest surfaces.

A deliberately small sliding-window limiter (no new dependencies): each
bucket keeps the timestamps of the last minute's hits. Two bucket kinds:

- per-sender  — one chat address (WhatsApp wa_id / SMS number) or one
                ingest credential, limited to OVERLAP_RATE_LIMIT_PER_MIN
                requests/minute (default 120, generous for a human).
- per-IP      — flood protection at 20x the per-sender limit. On the
                WhatsApp webhook almost everything arrives from Meta's
                egress IPs, so this bucket only trips on a real flood.

Configuration (environment):
- OVERLAP_RATE_LIMIT_PER_MIN  per-sender requests/minute (default 120)
- OVERLAP_RATE_LIMIT_OFF=1    kill switch: disable all limiting

State is per-process memory: it resets on restart and is not shared
across workers. That is the right size for this service today (single
uvicorn process); move to a shared store if it ever runs multi-worker.
"""

import os
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

_WINDOW_SECONDS = 60.0
_IP_MULTIPLIER = 20

_buckets: dict[str, deque] = {}
_lock = threading.Lock()


def _off() -> bool:
    return os.environ.get("OVERLAP_RATE_LIMIT_OFF") == "1"


def sender_limit_per_min() -> int:
    try:
        return max(1, int(os.environ.get("OVERLAP_RATE_LIMIT_PER_MIN", "120")))
    except ValueError:
        return 120


def ip_limit_per_min() -> int:
    return sender_limit_per_min() * _IP_MULTIPLIER


def _allow(key: str, limit: int, now: float) -> bool:
    with _lock:
        dq = _buckets.setdefault(key, deque())
        cutoff = now - _WINDOW_SECONDS
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


def reset() -> None:
    """Drop all buckets (used by tests)."""
    with _lock:
        _buckets.clear()


def client_ip(request: Request) -> str:
    """Best-effort source IP.

    The service runs behind one known proxy (Render/Fly), which appends
    X-Forwarded-For; its first hop is the client. Direct deployments fall
    back to the socket peer.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _too_many() -> HTTPException:
    return HTTPException(
        status_code=429, detail="Rate limit exceeded. Try again shortly."
    )


def enforce_ip(request: Request) -> None:
    if _off():
        return
    if not _allow(f"ip:{client_ip(request)}", ip_limit_per_min(), time.monotonic()):
        raise _too_many()


def enforce_sender(sender_key: str) -> None:
    if _off() or not sender_key:
        return
    if not _allow(f"sender:{sender_key}", sender_limit_per_min(), time.monotonic()):
        raise _too_many()


def enforce(request: Request, sender_key: str) -> None:
    """IP + sender checks in one call, for handlers that know both."""
    enforce_ip(request)
    enforce_sender(sender_key)
