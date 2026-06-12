"""
totp.py — RFC 6238 TOTP second factor (Spec 026, Phase 2).

Stdlib-only implementation: no pyotp, no external dependencies.

Public API:
    random_base32(length=32) -> str
        Generate a cryptographically random base32 secret.

    totp_now(secret_b32, t=None, step=30, digits=6) -> str
        Compute the current TOTP code (RFC 6238 / RFC 4226).

    verify(secret_b32, code, window=1) -> bool
        Accept codes within ±window steps (clock skew).  Constant-time compare.

    provisioning_uri(secret_b32, account, issuer) -> str
        Build the otpauth://totp/… URI for QR code generation.

    gen_recovery_codes(n=10) -> list[str]
        Generate n one-time recovery codes (human-friendly hex pairs).

    hash_code(code) -> str
        SHA-256 hex digest of a recovery code (for safe storage).

    verify_and_consume(code, hashes) -> (ok: bool, remaining: list[str])
        Check a recovery code against the stored hashes; remove on match.
"""

import hashlib
import hmac
import math
import secrets
import struct
import time
import urllib.parse
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Base32 alphabet (RFC 4648, uppercase, no padding needed for TOTP)
# ─────────────────────────────────────────────────────────────────────────────

_B32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def random_base32(length: int = 32) -> str:
    """Return a cryptographically random base32-encoded secret of *length* chars.

    Length must be a positive multiple of 8 for standard base32 blocks, but
    any length ≥ 16 works for TOTP (apps accept arbitrary-length secrets).
    The default of 32 characters gives 160 bits of entropy.
    """
    if length < 1:
        raise ValueError("length must be >= 1")
    return "".join(secrets.choice(_B32_ALPHABET) for _ in range(length))


# ─────────────────────────────────────────────────────────────────────────────
# TOTP core (RFC 6238 + RFC 4226)
# ─────────────────────────────────────────────────────────────────────────────

def _b32decode(secret_b32: str) -> bytes:
    """Decode a base32 string (case-insensitive, no-padding) to bytes."""
    # stdlib base64.b32decode requires uppercase and correct padding
    import base64
    s = secret_b32.upper().strip()
    # Add padding so length is a multiple of 8
    pad = (8 - len(s) % 8) % 8
    return base64.b32decode(s + "=" * pad)


def _hotp(key_bytes: bytes, counter: int, digits: int = 6) -> str:
    """Compute an HOTP value (RFC 4226 §5)."""
    # Pack counter as big-endian 8-byte integer
    msg = struct.pack(">Q", counter)
    # HMAC-SHA1
    h = hmac.new(key_bytes, msg, hashlib.sha1).digest()
    # Dynamic truncation
    offset = h[-1] & 0x0F
    code_int = struct.unpack(">I", h[offset: offset + 4])[0] & 0x7FFFFFFF
    # Reduce to `digits` digits
    code = code_int % (10 ** digits)
    return str(code).zfill(digits)


def totp_now(
    secret_b32: str,
    t: Optional[float] = None,
    step: int = 30,
    digits: int = 6,
) -> str:
    """Return the TOTP code for the given base32 secret at time *t* (default: now).

    Implements RFC 6238 (TOTP) on top of RFC 4226 (HOTP) with SHA-1.
    *step* is the time-step window in seconds (standard: 30).
    *digits* is the OTP length (standard: 6).
    """
    if t is None:
        t = time.time()
    key_bytes = _b32decode(secret_b32)
    counter = int(t) // step
    return _hotp(key_bytes, counter, digits)


def verify(
    secret_b32: str,
    code: str,
    window: int = 1,
    t: Optional[float] = None,
    step: int = 30,
    digits: int = 6,
) -> bool:
    """Verify a TOTP code within ±*window* steps of the current time.

    Uses hmac.compare_digest for constant-time comparison to prevent
    timing side-channels.  A window of 1 accepts the previous step,
    the current step, and the next step (covers ±30 s clock skew).
    """
    if not code or len(code) != digits or not code.isdigit():
        return False
    if t is None:
        t = time.time()
    key_bytes = _b32decode(secret_b32)
    current_counter = int(t) // step
    for delta in range(-window, window + 1):
        expected = _hotp(key_bytes, current_counter + delta, digits)
        if hmac.compare_digest(code, expected):
            return True
    return False


def provisioning_uri(secret_b32: str, account: str, issuer: str) -> str:
    """Build the otpauth://totp/… URI for QR code generation.

    Follows the Google Authenticator Key URI Format:
    https://github.com/google/google-authenticator/wiki/Key-Uri-Format
    """
    label = urllib.parse.quote(f"{issuer}:{account}", safe="")
    params = urllib.parse.urlencode({
        "secret": secret_b32.upper(),
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": 6,
        "period": 30,
    })
    return f"otpauth://totp/{label}?{params}"


# ─────────────────────────────────────────────────────────────────────────────
# Recovery codes
# ─────────────────────────────────────────────────────────────────────────────

def gen_recovery_codes(n: int = 10) -> list:
    """Generate *n* one-time recovery codes.

    Each code is 8 hex characters split into two groups of 4 (e.g. "a1b2-c3d4"),
    giving 32 bits of entropy per code — enough for a one-time emergency token.
    Returns a plain list of strings; these are shown to the operator ONCE.
    """
    codes = []
    for _ in range(n):
        raw = secrets.token_hex(4)  # 8 hex chars = 4 bytes = 32 bits
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes


def hash_code(code: str) -> str:
    """Return the SHA-256 hex digest of a recovery code (for safe storage).

    Stored hashes are compared against user input; the plaintext code is never
    persisted after enrollment.
    """
    return hashlib.sha256(code.strip().lower().encode("utf-8")).hexdigest()


def verify_and_consume(code: str, hashes: list) -> tuple:
    """Check a recovery code against a list of stored SHA-256 hashes.

    On match: removes the matched hash from the list and returns (True, remaining).
    On no match: returns (False, hashes) — the list is unchanged.

    The comparison is constant-time per hash via hmac.compare_digest.
    """
    candidate = hash_code(code)
    for i, h in enumerate(hashes):
        if hmac.compare_digest(candidate, h):
            remaining = hashes[:i] + hashes[i + 1:]
            return True, remaining
    return False, list(hashes)


# ─────────────────────────────────────────────────────────────────────────────
# RFC 6238 test vector self-check
# ─────────────────────────────────────────────────────────────────────────────

def _rfc6238_selftest() -> None:
    """Verify the implementation against the RFC 6238 Appendix B test vectors.

    RFC 6238 test vectors use the ASCII secret b"12345678901234567890" (20 bytes)
    encoded in base32 as "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ".

    Tested time stamps (Unix epoch) and expected 8-digit TOTP codes (SHA-1):
        t=59         → 94287082
        t=1111111109 → 07081804
        t=1111111111 → 14050471
        t=1234567890 → 89005924
        t=2000000000 → 69279037
        t=20000000000→ 65353130

    Source: RFC 6238 Appendix B (Table 1, TOTP Algorithm: SHA1 column).
    """
    # The RFC 6238 test secret in base32
    SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

    vectors = [
        (59,          "94287082"),
        (1111111109,  "07081804"),
        (1111111111,  "14050471"),
        (1234567890,  "89005924"),
        (2000000000,  "69279037"),
        (20000000000, "65353130"),
    ]

    for ts, expected in vectors:
        got = totp_now(SECRET_B32, t=ts, digits=8)
        assert got == expected, (
            f"RFC 6238 self-test FAILED at t={ts}: expected {expected!r}, got {got!r}"
        )


# Run self-test at import time (cheap — no I/O, pure computation).
_rfc6238_selftest()
