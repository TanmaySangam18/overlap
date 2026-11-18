"""Screenshot intake: parser heuristics, the /ingest/screenshot
endpoint, dedupe across overlapping screenshots, and the unconfirmed-
attribution confirmation flow through the chat agent.
"""

import os

import pytest
from fastapi.testclient import TestClient

from app import ratelimit, screenshot_parser, store, whatsapp as wa
from app.main import app
from app.normalize import normalize_identifier

client = TestClient(app)

PETER_WA = "15550107777"
PETER_HASH = normalize_identifier(PETER_WA)["hash"]
MIA_WA = "15550108888"
MIA_HASH = normalize_identifier(MIA_WA)["hash"]
SARAH = "15551110001"

WHATSAPP_OCR = """9:41
Peter
last seen today at 9:12 PM
hey, are we still on for tomorrow?
can't sleep, thinking about you
Yesterday
i loved last night
typing...
"""


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    os.environ["OVERLAP_DB_PATH"] = str(tmp_path / "shot-test.db")
    ratelimit.reset()
    wa.DRY_RUN_LOG.clear()
    yield


def make_token(owner_id):
    return client.post("/tokens", json={"owner_id": owner_id}).json()["ingest_token"]


def register(owner_id, label="Peter", number=PETER_WA):
    store.add_person(owner_id, label, normalize_identifier(number))


def post_shot(token, text, **extra):
    body = {"text": text, **extra}
    return client.post(
        "/ingest/screenshot", json=body, headers={"X-Overlap-Token": token}
    )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parser_header_match_and_noise_filtering():
    register("parser-owner")
    parsed = screenshot_parser.parse_screenshot("parser-owner", WHATSAPP_OCR)
    assert parsed["person"]["label"] == "Peter"
    assert parsed["confirmed"] is True
    texts = [m["text"] for m in parsed["messages"]]
    assert texts == [
        "hey, are we still on for tomorrow?",
        "can't sleep, thinking about you",
        "i loved last night",
    ]
    # Header line, status-bar time, last-seen, date divider, typing…
    assert parsed["noise_lines"] == 5
    assert parsed["duplicates"] == 0


def test_parser_burst_fallback_is_unconfirmed():
    register("burst-owner", "Peter", PETER_WA)
    register("burst-owner", "Mia", MIA_WA)
    store.set_agent_state(
        "burst-owner", "idle", {"last_person_hash": MIA_HASH}
    )
    ocr = "9:41\nPetey\nonline\nmiss u already\n"
    parsed = screenshot_parser.parse_screenshot("burst-owner", ocr)
    assert parsed["person"]["label"] == "Mia"
    assert parsed["confirmed"] is False
    assert "miss u already" in [m["text"] for m in parsed["messages"]]


def test_parser_no_persons_registered():
    parsed = screenshot_parser.parse_screenshot("ghost-owner", WHATSAPP_OCR)
    assert parsed["person"] is None
    assert parsed["messages"] == []


# ---------------------------------------------------------------------------
# Endpoint: auth, filing, matching
# ---------------------------------------------------------------------------


def test_screenshot_requires_valid_token():
    assert post_shot("", WHATSAPP_OCR).status_code == 401
    assert post_shot("ovl_not-a-real-token", WHATSAPP_OCR).status_code == 401


def test_screenshot_without_registered_person_is_422():
    token = make_token("nobody-owner")
    r = post_shot(token, WHATSAPP_OCR)
    assert r.status_code == 422


def test_screenshot_files_signals_and_contributes_to_match():
    tok_a = make_token("shot-sarah")
    register("shot-sarah")
    r = post_shot(tok_a, WHATSAPP_OCR, source_app="whatsapp")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["attribution"] == "header"
    assert body["filed"] == 3
    assert body["potential_match"] is None

    signals = store.signals_for_hash(PETER_HASH)
    assert len(signals) == 3
    assert {s["source"] for s in signals} == {"screenshot"}

    tok_b = make_token("shot-luna")
    register("shot-luna")
    rb = post_shot(tok_b, "9:41\nPeter\nActive now\nyou up?\n")
    assert rb.status_code == 201, rb.text
    match = rb.json()["potential_match"]
    assert match is not None
    assert match["distinct_owner_count"] == 2
    assert {e["source"] for e in match["evidence"]} == {"screenshot"}

    listed = client.get("/matches", params={"owner_id": "shot-sarah"}).json()
    assert len(listed["matches"]) == 1


def test_screenshot_dedupe_across_overlapping_screenshots():
    token = make_token("dedupe-owner")
    register("dedupe-owner")
    shot1 = "Peter\nonline\nalpha message one\nbravo message two\ncharlie message three\n"
    r1 = post_shot(token, shot1)
    assert r1.json()["filed"] == 3
    assert r1.json()["duplicates_skipped"] == 0

    shot2 = "Peter\nonline\nbravo message two\ncharlie message three\ndelta message four\n"
    r2 = post_shot(token, shot2)
    assert r2.json()["filed"] == 1
    assert r2.json()["duplicates_skipped"] == 2

    pairs = store.pair_messages_for("dedupe-owner", PETER_HASH)
    assert len(pairs) == 4
    assert {p["source"] for p in pairs} == {"screenshot"}


def test_screenshot_rate_limit_applies(monkeypatch):
    monkeypatch.setenv("OVERLAP_RATE_LIMIT_PER_MIN", "2")
    ratelimit.reset()
    token = make_token("rl-shot-owner")
    register("rl-shot-owner")
    codes = [post_shot(token, WHATSAPP_OCR).status_code for _ in range(3)]
    assert codes == [201, 201, 429]


# ---------------------------------------------------------------------------
# Unconfirmed attribution: staged, noticed, confirmed in chat
# ---------------------------------------------------------------------------

_counter = [5000]


def wa_payload(wa_id, name, message):
    _counter[0] += 1
    message.setdefault("id", f"wamid.shot{_counter[0]}")
    message.setdefault("from", wa_id)
    message.setdefault("timestamp", str(1767225600 + _counter[0]))
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": wa_id, "profile": {"name": name}}],
                            "messages": [message],
                        }
                    }
                ]
            }
        ],
    }


def wa_send(wa_id, name, message):
    r = client.post("/webhooks/whatsapp", json=wa_payload(wa_id, name, message))
    assert r.status_code == 200, r.text
    return [rec for rec in wa.DRY_RUN_LOG if rec["to"] == wa_id]


def test_unconfirmed_screenshot_stages_notice_and_confirms():
    # Sarah onboards in WhatsApp and registers Peter.
    wa_send(SARAH, "Sarah", {"type": "text", "text": {"body": "hi"}})
    wa_send(SARAH, "Sarah", {"type": "text", "text": {"body": "YES"}})
    wa_send(
        SARAH,
        "Sarah",
        {
            "type": "contacts",
            "contacts": [
                {
                    "name": {"formatted_name": "Peter"},
                    "phones": [{"phone": "+1 555-010-7777", "wa_id": PETER_WA}],
                }
            ],
        },
    )
    # One forward: attributed to Peter, and he becomes her most recent.
    wa_send(
        SARAH,
        "Sarah",
        {"type": "text", "text": {"body": "hey you"}, "context": {"forwarded": True}},
    )
    owner = store.owner_id_for_address("whatsapp", SARAH)
    token = make_token(owner)

    # A screenshot whose header OCR garbled the name: nothing files yet.
    ocr = "9:41\nPetey\nonline\nscreenshots don't lie\nsee you at mine tonight\n"
    r = post_shot(token, ocr)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "awaiting_confirmation"
    assert body["attribution"] == "unconfirmed"
    assert body["filed"] == 0
    assert body["staged"] == 3  # garbled header line + two messages
    assert body["person"]["label"] == "Peter"
    mine = [
        s for s in store.signals_for_hash(PETER_HASH) if s["owner_id"] == owner
    ]
    assert {s["source"] for s in mine} == {"whatsapp"}

    # The agent owes her a notice naming the guess, guess button first.
    outbox = store.drain_outbox(channel="whatsapp")
    assert len(outbox) == 1
    assert "best guess is Peter" in outbox[0]["text"]
    assert outbox[0]["buttons"][0]["id"] == f"attr:{PETER_HASH[:12]}"

    # She taps Peter: the staged snippets file as screenshot-sourced.
    wa.DRY_RUN_LOG.clear()
    wa_send(
        SARAH,
        "Sarah",
        {
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {
                    "id": f"attr:{PETER_HASH[:12]}",
                    "title": "Peter",
                },
            },
        },
    )
    assert any(
        "✓ Logged 3 messages for Peter" in rec["text"]
        for rec in wa.DRY_RUN_LOG
        if rec["to"] == SARAH
    )
    mine = [
        s for s in store.signals_for_hash(PETER_HASH) if s["owner_id"] == owner
    ]
    shot = [s for s in mine if s["source"] == "screenshot"]
    assert len(shot) == 3
    assert "screenshots don't lie" in {s["message_snippet"] for s in shot}
