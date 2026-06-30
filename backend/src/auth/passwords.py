"""Local password hashing — stdlib only (PBKDF2-HMAC-SHA256).

We deliberately avoid bcrypt/argon2 so there's no native dependency to vendor
into the frozen Windows build. PBKDF2-HMAC-SHA256 at a high iteration count is
a sound, widely-used choice (OWASP-recommended when bcrypt/argon2 aren't
available).

Stored format (single string, self-describing so the cost can evolve):
    pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 600_000  # OWASP 2023 guidance for PBKDF2-HMAC-SHA256
_SALT_BYTES = 16


def hash_password(plain: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, _ITERATIONS)
    return "{}${}${}${}".format(
        _ALGO,
        _ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(plain: str, stored: str) -> bool:
    """Constant-time verify. Returns False on any malformed/empty stored hash."""
    if not stored:
        return False
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        iterations = int(iters_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, base64.binascii.Error):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)
