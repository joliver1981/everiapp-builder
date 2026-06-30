"""Tests for the license key system."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from src.licensing import license as lic


def test_issue_and_parse_round_trip():
    token = lic.issue_license(
        sub="Acme Corp", seats=50, tier="pro",
        days_valid=365, features=["app_db", "datasets"],
    )
    info = lic.parse_license_token(token)
    assert info.is_active
    assert info.sub == "Acme Corp"
    assert info.tier == "pro"
    assert info.seats == 50
    assert "app_db" in info.features


def test_perpetual_license_never_expires():
    token = lic.issue_license(sub="Forever Inc", days_valid=0)
    info = lic.parse_license_token(token)
    assert info.is_active
    assert info.is_perpetual
    assert info.days_remaining is None


def test_expired_license_reports_expired():
    token = lic.issue_license(sub="Past Co", days_valid=1)
    info = lic.parse_license_token(token)
    assert info.is_active

    # Forge an expired one by manually building a JWT with past expires_at
    import jwt
    payload = {
        "sub": "Old Co", "license_id": "old", "issued_at": 0,
        "expires_at": int(time.time()) - 100,
        "seats": 1, "tier": "trial", "features": [],
    }
    bad_token = jwt.encode(payload, lic.LICENSE_SIGNING_SECRET, algorithm=lic.LICENSE_ALG)
    info = lic.parse_license_token(bad_token)
    assert info.status == "expired"
    assert not info.is_active


def test_bad_signature_reports_invalid():
    token = lic.issue_license(sub="X", days_valid=10)
    tampered = token[:-5] + "AAAAA"
    info = lic.parse_license_token(tampered)
    assert info.status == "invalid"
    assert not info.is_active


def test_garbage_token_reports_invalid():
    info = lic.parse_license_token("not-a-jwt")
    assert info.status == "invalid"
    assert not info.is_active


def test_has_feature_gating():
    token = lic.issue_license(sub="X", tier="starter", features=["app_db"])
    info = lic.parse_license_token(token)
    assert info.has_feature("app_db")
    assert not info.has_feature("enterprise_sso")


def test_all_feature_unlocks_everything():
    token = lic.issue_license(sub="X", features=["all"])
    info = lic.parse_license_token(token)
    assert info.has_feature("anything_we_want")
    assert info.has_feature("does_not_exist_yet")


def test_load_license_from_file(tmp_path, monkeypatch):
    # Issue a license, drop it at <tmp>/data/license.key, load via load_license
    monkeypatch.delenv("AIHUB_LICENSE", raising=False)
    token = lic.issue_license(sub="FileCo", days_valid=10)
    (tmp_path / "license.key").write_text(token)
    info = lic.load_license(tmp_path)
    assert info.sub == "FileCo"


def test_load_license_from_env(tmp_path, monkeypatch):
    token = lic.issue_license(sub="EnvCo", days_valid=10)
    monkeypatch.setenv("AIHUB_LICENSE", token)
    info = lic.load_license(tmp_path)
    assert info.sub == "EnvCo"


def test_load_license_falls_back_to_unlicensed(tmp_path, monkeypatch):
    monkeypatch.delenv("AIHUB_LICENSE", raising=False)
    info = lic.load_license(tmp_path)
    assert info.status == "unlicensed"
    assert not info.is_active


# --- CLI smoke checks (don't run the actual platform) ----------------------
def test_cli_license_show_smoke(tmp_path, monkeypatch):
    from click.testing import CliRunner

    monkeypatch.setenv("AIHUB_LICENSE", lic.issue_license(sub="ShowCo", days_valid=10))
    # Reset the cached license so show picks up the new env var
    lic.set_current_license(lic.load_license(tmp_path))

    from src.cli import cli
    result = CliRunner().invoke(cli, ["license", "show"])
    assert result.exit_code == 0
    assert "ShowCo" in result.output


def test_cli_license_issue_writes_token(tmp_path):
    from click.testing import CliRunner

    out_path = tmp_path / "issued.key"
    from src.cli import cli
    result = CliRunner().invoke(cli, [
        "license", "issue",
        "--customer", "IssuedCo",
        "--seats", "10",
        "--tier", "pro",
        "--days", "30",
        "--feature", "app_db",
        "--save-to", str(out_path),
    ])
    assert result.exit_code == 0
    assert out_path.exists()
    token = out_path.read_text().strip()
    info = lic.parse_license_token(token)
    assert info.sub == "IssuedCo"
    assert info.tier == "pro"
    assert info.seats == 10
