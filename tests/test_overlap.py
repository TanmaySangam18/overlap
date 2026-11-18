import os
import tempfile

os.environ["OVERLAP_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="overlap-test-"), "test.db"
)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.normalize import normalize_identifier  # noqa: E402

client = TestClient(app)

SIGNAL = {
    "source": "whatsapp",
    "conversation_id": "chat-1",
    "message_text": "I love you ❤️",
    "sent_at": "2026-11-17T14:30:00Z",
    "consent_confirmed": True,
}


def submit(owner, identifier, **overrides):
    body = {**SIGNAL, "owner_id": owner, "sender_identifier": identifier, **overrides}
    return client.post("/signals", json=body)


def test_phone_formats_normalize_to_same_hash():
    hashes = {
        normalize_identifier(v)["hash"]
        for v in ["+15551234567", "+1 (555) 123-4567", "5551234567", "1-555-123-4567"]
    }
    assert len(hashes) == 1


def test_gmail_variants_normalize_to_same_hash():
    a = normalize_identifier("John.Doe+news@gmail.com")["hash"]
    b = normalize_identifier("johndoe@gmail.com")["hash"]
    assert a == b


def test_consent_is_required():
    r = submit("bryan", "+15551234567", consent_confirmed=False)
    assert r.status_code == 400


def test_invalid_identifier_rejected():
    r = submit("bryan", "not-a-phone-or-email")
    assert r.status_code == 422


def test_no_match_for_single_owner_even_with_two_signals():
    r1 = submit("solo", "+15550001111")
    assert r1.status_code == 201
    assert r1.json()["potential_match"] is None
    r2 = submit("solo", "+1 555 000 1111", conversation_id="chat-2")
    assert r2.json()["potential_match"] is None


def test_overlap_match_across_two_owners_hides_other_owner():
    r1 = submit("bryan", "+15551234567")
    assert r1.json()["potential_match"] is None

    r2 = submit("sarah", "(555) 123-4567", conversation_id="chat-9")
    assert r2.status_code == 201
    match = r2.json()["potential_match"]
    assert match is not None
    assert match["distinct_owner_count"] == 2
    assert match["identifier"]["masked"] == "+***4567"
    # Minimal evidence: the other owner is never identified by id.
    assert "bryan" not in str(match["evidence"])
    assert {e["submitted_by"] for e in match["evidence"]} == {"you", "another user"}

    listed = client.get("/matches", params={"owner_id": "bryan"}).json()
    assert len(listed["matches"]) == 1
    assert listed["matches"][0]["match_id"] == match["match_id"]


def test_alert_requires_confirmed_review():
    submit("peter", "+15559998888")
    r = submit("bryan2", "+1 555 999 8888", conversation_id="chat-3")
    match_id = r.json()["potential_match"]["match_id"]

    # No review yet -> alert blocked.
    blocked = client.post(f"/matches/{match_id}/alert", json={"owner_id": "bryan2"})
    assert blocked.status_code == 409

    # Dismissed review -> still blocked.
    client.post(
        f"/matches/{match_id}/review",
        json={"owner_id": "bryan2", "decision": "dismissed"},
    )
    blocked = client.post(f"/matches/{match_id}/alert", json={"owner_id": "bryan2"})
    assert blocked.status_code == 409

    # Confirmed review -> alert recorded internally, nothing sent out.
    client.post(
        f"/matches/{match_id}/review",
        json={"owner_id": "bryan2", "decision": "confirmed"},
    )
    ok = client.post(f"/matches/{match_id}/alert", json={"owner_id": "bryan2"})
    assert ok.status_code == 201
    assert ok.json()["status"] == "recorded"
    assert "No message was sent" in ok.json()["delivery"]


def test_non_participant_cannot_see_match():
    submit("owner-a", "+15557776666")
    r = submit("owner-b", "+15557776666", conversation_id="chat-4")
    match_id = r.json()["potential_match"]["match_id"]
    stranger = client.get(f"/matches/{match_id}", params={"owner_id": "owner-c"})
    assert stranger.status_code == 404


def make_token(owner):
    r = client.post("/tokens", json={"owner_id": owner})
    assert r.status_code == 201
    return r.json()["ingest_token"]


def ingest(token, identifier, text="hi", **overrides):
    body = {"sender_identifier": identifier, "message_text": text, **overrides}
    return client.post(
        "/ingest/shortcut", json=body, headers={"X-Overlap-Token": token}
    )


def test_shortcut_ingest_requires_valid_token():
    bad = ingest("ovl_not-a-real-token", "+15550101010")
    assert bad.status_code == 401


def test_confession_accepted_sets_together():
    from app.events import detect_relationship

    tl = [
        {
            "from": "peter",
            "text": "I'm in love with you. Will you be my girlfriend?",
            "sent_at": "2026-10-03T20:15:00-05:00",
        },
        {
            "from": "sarah",
            "text": "Yes!! I love you too",
            "sent_at": "2026-10-03T20:22:00-05:00",
        },
    ]
    r = detect_relationship(tl)
    assert r["state"] == "together"
    assert r["since"] == tl[0]["sent_at"]


def test_confession_rejected_sets_not_together():
    from app.events import detect_relationship

    tl = [
        {
            "from": "peter",
            "text": "I'm in love with you. Will you be my girlfriend?",
            "sent_at": "2026-10-03T20:15:00-05:00",
        },
        {
            "from": "sarah",
            "text": "I don't feel the same, let's just be friends",
            "sent_at": "2026-10-03T20:22:00-05:00",
        },
    ]
    assert detect_relationship(tl)["state"] == "not_together"


def test_plain_ily_is_not_a_confession():
    from app.events import detect_relationship

    tl = [
        {
            "from": "peter",
            "text": "ily, thanks for covering my shift",
            "sent_at": "2026-09-10T15:30:00-05:00",
        }
    ]
    assert detect_relationship(tl)["detected"] is False


def test_flagging_spike_flags_and_calm_does_not():
    from app import store as _store
    from app.flagging import evaluate
    from app.normalize import normalize_identifier as _norm

    partner = "+15550003333"

    def seed(owner, text, iso):
        _store.add_signal(owner, "imessage", f"chat-{owner}", _norm(partner), text, iso)

    for owner in ("t-luna", "t-emma"):
        seed(owner, "did you see the game last night", "2026-09-08T14:00:00-05:00")
        seed(owner, "ily for covering my shift lol", "2026-09-10T15:30:00-05:00")
        seed(owner, "happy birthday!!", "2026-09-15T13:00:00-05:00")
        seed(owner, "that class was brutal", "2026-09-22T16:00:00-05:00")
        seed(owner, "ily you're the best", "2026-09-29T14:30:00-05:00")

    for d in [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 20, 21, 22, 24, 25, 26, 27]:
        seed("t-luna", "can't sleep, thinking about you", f"2026-10-{d:02d}T01:10:00-05:00")

    seed("t-emma", "that exam was rough", "2026-10-07T14:00:00-05:00")
    seed("t-emma", "ily thanks for the notes", "2026-10-14T15:00:00-05:00")
    seed("t-emma", "see you at sam's on friday", "2026-10-21T13:30:00-05:00")

    result = evaluate("t-sarah", partner, "2026-10-03T20:15:00-05:00")
    by_owner = {r["contact_owner"]: r for r in result["reports"]}
    assert by_owner["t-luna"]["flagged"] is True
    assert by_owner["t-emma"]["flagged"] is False


def test_shortcut_ingest_creates_imessage_match_across_phones():
    sarah_token = make_token("iphone-sarah")
    emma_token = make_token("iphone-emma")

    r1 = ingest(sarah_token, "+15550101010", text="I love you ❤️")
    assert r1.status_code == 201
    assert r1.json()["ingested_via"] == "ios_shortcuts_automation"
    assert r1.json()["potential_match"] is None

    r2 = ingest(emma_token, "555-010-1010", text="see you tonight")
    assert r2.status_code == 201
    match = r2.json()["potential_match"]
    assert match is not None
    assert match["distinct_owner_count"] == 2
    assert {e["source"] for e in match["evidence"]} == {"imessage"}
