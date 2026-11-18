"""Sender identifier normalization and privacy-preserving matching keys.

The raw identifier is canonicalized (so the same person written different
ways produces the same value), then hashed. Only the hash and a masked
display form are stored; matching happens on the hash.

In production the hash is an HMAC-SHA256 keyed with OVERLAP_HASH_PEPPER
(a server-side secret): the phone-number space is small enough that a
plain SHA-256 of a number is brute-forceable, so an unkeyed hash is
effectively the number. The pepper keeps cross-user matching intact
(every owner hashes under the same key) while making the stored hash
useless to anyone who doesn't hold the secret. Without the env var
(local dev / tests) the digest falls back to plain SHA-256.
"""

import hashlib
import hmac
import os
import re


def digest_for(value: str) -> str:
    """The matching digest for a canonical value (peppered in prod)."""
    pepper = os.environ.get("OVERLAP_HASH_PEPPER", "")
    if pepper:
        return hmac.new(
            pepper.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_identifier(raw: str) -> dict:
    value = (raw or "").strip()
    if not value:
        raise ValueError("sender_identifier is empty")
    # Telegram user id, e.g. "telegram:123456789" — the stable identifier
    # for a Telegram sender when no phone number is visible (the MTProto
    # user client, scripts/telegram_client.py, files senders this way).
    # It matches across owners because the same Telegram account has the
    # same id in everyone's chats, exactly like a phone number does.
    low = value.lower()
    if low.startswith("telegram:") or low.startswith("tg:"):
        digits = re.sub(r"\D", "", value.split(":", 1)[1])
        if not digits:
            raise ValueError("invalid telegram identifier")
        canonical = f"telegram:{digits}"
        digest = digest_for(canonical)
        return {
            "type": "telegram",
            "canonical": canonical,
            "hash": digest,
            "masked": f"tg:***{digits[-4:]}",
        }
    if "@" in value:
        canonical = _normalize_email(value)
        kind = "email"
    else:
        canonical = _normalize_phone(value)
        kind = "phone"
    digest = digest_for(canonical)
    return {
        "type": kind,
        "canonical": canonical,
        "hash": digest,
        "masked": _mask(kind, canonical),
    }


def _normalize_email(value: str) -> str:
    email = value.strip().lower()
    if email.count("@") != 1:
        raise ValueError("invalid email address")
    local, domain = email.split("@")
    if not local or "." not in domain:
        raise ValueError("invalid email address")
    # Gmail ignores dots in the local part and anything after a '+'.
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+")[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def _normalize_phone(value: str) -> str:
    v = value.strip()
    if v.startswith("00"):  # international dialing prefix
        v = "+" + v[2:]
    digits = re.sub(r"\D", "", v)
    if not digits:
        raise ValueError("invalid phone number")
    # Bare 10-digit numbers are treated as US/Canada (+1). Numbers that
    # already carry a country code are kept as dialed.
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) < 7 or len(digits) > 15:
        raise ValueError("invalid phone number length")
    return "+" + digits


def _mask(kind: str, canonical: str) -> str:
    if kind == "phone":
        return "+***" + canonical[-4:]
    if kind == "telegram":
        return f"tg:***{canonical[-4:]}"
    local, domain = canonical.split("@")
    return f"{local[0]}***@{domain}"
