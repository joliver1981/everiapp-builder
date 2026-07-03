"""Vendored-SDK drift lock: app-template/src/sdk must equal app-sdk/src.

Generated apps do NOT install @aihub/app-sdk from a registry — the scaffold
copies app-template/ (including src/sdk/) into the draft, and the template's
vite config aliases '@aihub/app-sdk' to that vendored copy. So a fix that
lands only in app-sdk/ silently never reaches generated apps.

This happened: the Wave-1 fix that made useAppConfig send
`Authorization: Bearer window.__AIHUB_TOKEN__` landed in app-sdk/ only, so
every generated app's config fetch 401'd against the bearer-only
/settings/resolved endpoint and useAppConfig() resolved to {} — while all
HTTP-level tests stayed green because they hit the endpoint directly.

If this test fails: sync the two copies (they must be byte-identical), and
remember existing apps under data/apps carry their own scaffolded snapshot.
"""
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL = _REPO_ROOT / "app-sdk" / "src"
_VENDORED = _REPO_ROOT / "app-template" / "src" / "sdk"


def _files(root: Path) -> set[str]:
    return {p.name for p in root.iterdir() if p.is_file()}


def test_same_file_sets():
    assert _files(_CANONICAL) == _files(_VENDORED), (
        "app-template/src/sdk and app-sdk/src carry different file sets — "
        "the vendored copy generated apps run has drifted from the canonical SDK"
    )


@pytest.mark.parametrize("name", sorted(_files(_CANONICAL)))
def test_vendored_file_matches_canonical(name: str):
    canonical = (_CANONICAL / name).read_text(encoding="utf-8").replace("\r\n", "\n")
    vendored = (_VENDORED / name).read_text(encoding="utf-8").replace("\r\n", "\n")
    assert canonical == vendored, (
        f"app-template/src/sdk/{name} differs from app-sdk/src/{name} — "
        "generated apps run the vendored copy, so the fix in app-sdk/ never "
        "reaches them; make the files identical"
    )


def test_config_fetch_sends_bearer_token():
    # The specific Wave-1 regression: the config fetch must send the token the
    # runtime proxy injects, because /settings/resolved is bearer-only.
    src = (_VENDORED / "useAppConfig.ts").read_text(encoding="utf-8")
    assert "__AIHUB_TOKEN__" in src, "vendored useAppConfig no longer reads window.__AIHUB_TOKEN__"
    assert "Authorization" in src, "vendored useAppConfig no longer sends an Authorization header"
