"""License key system — JWT-signed, offline-verifiable, per-install.

A license key is a JWT (HS256 with a publisher-only secret for now; the prod
build will switch to RS256 with a published verify key so we don't need to
ship our signing secret).

Claims:
    sub          customer/org name
    license_id   unique license id
    issued_at    epoch seconds
    expires_at   epoch seconds  (0 = perpetual)
    seats        max named users (0 = unlimited)
    tier         "trial" | "starter" | "pro" | "enterprise"
    features     list of feature flags this license unlocks
    fingerprint  optional install fingerprint binding

The license is loaded from one of (in order):
    1. AIHUB_LICENSE env var (the JWT itself)
    2. data/license.key  (file containing the JWT)
    3. None → unlicensed mode (read-only, no AI generation)

Verification happens at startup. If the license is expired or invalid the
platform still boots but enters DEGRADED mode:
  - AI generation disabled
  - Read-only on connections/datasets/apps
  - Admin can paste a new license to recover

The 'enterprise' tier never expires by default (expires_at=0).
"""
from __future__ import annotations

import dataclasses
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jwt

logger = logging.getLogger(__name__)


# For dev / self-issued licenses we use HS256 with a well-known secret.
# Production builds will replace this with an RS256 public key embedded in
# the binary. The HS256 secret here is the SAME on every install — that's
# fine for dev/self-issuance but customers must replace it for production
# revocation to work.
LICENSE_SIGNING_SECRET = os.environ.get(
    "AIHUB_LICENSE_SIGNING_SECRET",
    "aihub-license-dev-secret-CHANGE-FOR-PROD",
)
LICENSE_ALG = "HS256"


@dataclasses.dataclass
class LicenseInfo:
    """The parsed, validated license. Returned by `current_license()`."""
    sub: str
    license_id: str
    issued_at: int
    expires_at: int            # 0 = perpetual
    seats: int                 # 0 = unlimited
    tier: str                  # trial | starter | pro | enterprise
    features: list[str]
    fingerprint: str | None = None
    # Synthetic — populated after parse
    status: str = "valid"       # valid | expired | invalid | unlicensed
    issue: str | None = None    # human-readable reason if not valid

    @property
    def is_perpetual(self) -> bool:
        return self.expires_at == 0

    @property
    def days_remaining(self) -> int | None:
        if self.is_perpetual:
            return None
        return max(0, int((self.expires_at - time.time()) / 86400))

    @property
    def is_active(self) -> bool:
        return self.status == "valid"

    def has_feature(self, name: str) -> bool:
        return self.is_active and (
            name in self.features or "all" in self.features
        )

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["is_perpetual"] = self.is_perpetual
        d["days_remaining"] = self.days_remaining
        d["is_active"] = self.is_active
        return d


# ---------------------------------------------------------------------------
# Load + validate
# ---------------------------------------------------------------------------
def _decode(token: str) -> dict[str, Any]:
    """Decode the JWT (raises jwt.* on invalid signature/format)."""
    return jwt.decode(
        token,
        LICENSE_SIGNING_SECRET,
        algorithms=[LICENSE_ALG],
        options={"verify_signature": True, "require": ["sub", "license_id"]},
    )


def parse_license_token(token: str) -> LicenseInfo:
    """Decode + validate a license JWT. Always returns a LicenseInfo, even
    for invalid licenses (with status=invalid|expired)."""
    try:
        payload = _decode(token)
    except jwt.ExpiredSignatureError:
        return LicenseInfo(
            sub="(invalid)", license_id="(invalid)", issued_at=0,
            expires_at=0, seats=0, tier="invalid", features=[],
            status="expired", issue="License signature has expired",
        )
    except jwt.PyJWTError as e:
        return LicenseInfo(
            sub="(invalid)", license_id="(invalid)", issued_at=0,
            expires_at=0, seats=0, tier="invalid", features=[],
            status="invalid", issue=f"License decode failed: {e}",
        )

    info = LicenseInfo(
        sub=payload.get("sub", "(unknown)"),
        license_id=payload.get("license_id", "(unknown)"),
        issued_at=int(payload.get("issued_at", 0)),
        expires_at=int(payload.get("expires_at", 0)),
        seats=int(payload.get("seats", 0)),
        tier=str(payload.get("tier", "trial")),
        features=list(payload.get("features", [])),
        fingerprint=payload.get("fingerprint"),
    )

    # Expiry check
    if info.expires_at and info.expires_at < int(time.time()):
        info.status = "expired"
        info.issue = (
            f"License expired on {datetime.fromtimestamp(info.expires_at, timezone.utc).date()}"
        )
        return info

    return info


def _unlicensed() -> LicenseInfo:
    """Synthetic stand-in for the no-license state."""
    return LicenseInfo(
        sub="(no license)", license_id="(none)",
        issued_at=0, expires_at=0,
        seats=0, tier="unlicensed", features=[],
        status="unlicensed",
        issue=(
            "No license loaded. Set AIHUB_LICENSE env var, or place the "
            "license JWT in data/license.key, then restart the platform."
        ),
    )


def load_license(data_dir: str | Path) -> LicenseInfo:
    """Load + validate the license from (env -> file -> unlicensed). Used by
    startup wiring."""
    token = os.environ.get("AIHUB_LICENSE", "").strip()
    if not token:
        key_path = Path(data_dir) / "license.key"
        if key_path.exists():
            try:
                token = key_path.read_text(encoding="utf-8").strip()
            except Exception as e:
                logger.warning("Could not read license file %s: %s", key_path, e)

    if not token:
        return _unlicensed()

    info = parse_license_token(token)
    if info.is_active:
        logger.info(
            "License loaded: customer=%s tier=%s seats=%s expires=%s",
            info.sub, info.tier, info.seats or "unlimited",
            "perpetual" if info.is_perpetual else
            datetime.fromtimestamp(info.expires_at, timezone.utc).date(),
        )
    else:
        logger.warning("License %s — %s", info.status, info.issue)
    return info


# ---------------------------------------------------------------------------
# Issuing licenses (used by aihub license issue + tests)
# ---------------------------------------------------------------------------
def issue_license(
    *,
    sub: str,
    seats: int = 0,
    tier: str = "trial",
    days_valid: int = 30,
    features: list[str] | None = None,
    fingerprint: str | None = None,
    license_id: str | None = None,
) -> str:
    """Build + sign a license JWT. The CLI surfaces this for self-issue
    in dev; production licenses come from our license server.
    """
    import uuid

    now = int(time.time())
    expires_at = 0 if days_valid <= 0 else (now + days_valid * 86400)
    payload = {
        "sub": sub,
        "license_id": license_id or str(uuid.uuid4()),
        "issued_at": now,
        "expires_at": expires_at,
        "seats": int(seats),
        "tier": tier,
        "features": features or [],
    }
    if fingerprint:
        payload["fingerprint"] = fingerprint
    # PyJWT >=2.0 returns str, not bytes
    return jwt.encode(payload, LICENSE_SIGNING_SECRET, algorithm=LICENSE_ALG)


# ---------------------------------------------------------------------------
# Module-level current license — set once at startup
# ---------------------------------------------------------------------------
_current: LicenseInfo | None = None


def current_license() -> LicenseInfo:
    """Return the active license. Initializes lazily from settings on first call."""
    global _current
    if _current is None:
        from ..config import settings
        _current = load_license(settings.app_data_dir)
    return _current


def set_current_license(info: LicenseInfo) -> None:
    """Override the active license (used by tests + the 'load new license'
    admin endpoint)."""
    global _current
    _current = info
