"""Suite-wide test configuration.

The owner-scoped REST API (/signals, /matches, /tokens) requires the
operator token (OVERLAP_API_TOKEN) or the owner's own ingest token —
owner ids alone are derivable and prove nothing. The suite runs as
the operator: a known token is set here and every FastAPI TestClient
presents it by default, so existing call sites keep working.
tests/test_api_auth.py uses the raw Starlette client to exercise the
unauthenticated and wrong-credential paths.
"""

import os

os.environ.setdefault("OVERLAP_API_TOKEN", "test-operator-token")
# The app's daily maintenance loop (retention purge + timestamp
# anchoring) must never run during tests: anchoring makes a real
# network call and would mutate fixture databases nondeterministically.
os.environ.setdefault("OVERLAP_MAINTENANCE_OFF", "1")

import fastapi.testclient as _ftc

_RealTestClient = _ftc.TestClient


class _OperatorTestClient(_RealTestClient):
    def request(self, *args, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        headers.setdefault("X-Overlap-Admin", os.environ["OVERLAP_API_TOKEN"])
        kwargs["headers"] = headers
        return super().request(*args, **kwargs)


_ftc.TestClient = _OperatorTestClient
