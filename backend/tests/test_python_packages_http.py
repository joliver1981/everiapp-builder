"""Phase 2.1: admin-managed Python packages for server functions.

Real HTTP routes (TestClient, real auth). pip is faked at the service seam
(_run_pip) so no network is touched — the fake writes {name}-{ver}.dist-info
into the pip --target dir parsed from the args, which is exactly what the
inventory scanner reads. The managed dir is monkeypatched per-module to a temp
dir so runs never touch a real environment.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_python_packages.db"
if _DB.exists():
    _DB.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_DB}")
os.environ.setdefault("APP_DATA_DIR", str(_TMP / "apps_python_packages"))
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "python-packages-test")

from src.database import init_db  # noqa: E402
from src.main import app as fastapi_app  # noqa: E402
import src.python_env as python_env  # noqa: E402
import src.python_packages.service as pkg_service  # noqa: E402

# Isolated managed dir for this MODULE (other modules must never see our
# fakes). Patched at import so even module fixtures resolve here; settings
# paths elsewhere are still read live (v0.14.0 lesson) — this dir is ours.
_MANAGED = _TMP / f"server-packages-{uuid.uuid4().hex[:6]}" / "server-packages"
python_env.managed_packages_dir = lambda: _MANAGED  # type: ignore[assignment]


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(fastapi_app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _write_dist_info(target: Path, name: str, version: str) -> None:
    d = target / f"{name}-{version}.dist-info"
    d.mkdir(parents=True, exist_ok=True)
    (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    (target / name.replace("-", "_")).mkdir(exist_ok=True)


def _parse_pip_args(args: list[str]) -> tuple[Path, list[str]]:
    """(target, specs) from a pip argv — value-taking options are skipped so an
    --index-url URL is never mistaken for a spec."""
    tail = args[args.index("install") + 1:]
    target: Path | None = None
    specs: list[str] = []
    it = iter(tail)
    for a in it:
        if a == "--target":
            target = Path(next(it))
        elif a == "--index-url":
            next(it)
        elif a.startswith("-"):
            continue
        else:
            specs.append(a)
    assert target is not None, f"pip args missing --target: {args}"
    return target, specs


@pytest.fixture()
def fake_pip(monkeypatch):
    """Fake the pip subprocess seam. calls[] records argv; behavior is scripted
    via the returned dict (rc/out/block/version)."""
    state = {"rc": 0, "out": "ok", "calls": [], "version": "1.0.0", "block": None}

    def _fake(args: list[str]) -> tuple[int, str]:
        state["calls"].append(args)
        if state["block"] is not None:
            state["block"].wait(timeout=15)
        if state["rc"] == 0:
            target, specs = _parse_pip_args(args)
            target.mkdir(parents=True, exist_ok=True)
            for spec in specs:
                name, _, pin = spec.partition("==")
                _write_dist_info(target, pkg_service._normalize(name), pin or state["version"])
        return state["rc"], state["out"]

    monkeypatch.setattr(pkg_service, "_run_pip", _fake)
    return state


def _poll_terminal(client, admin, name: str, timeout_s: float = 10.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        inv = client.get("/api/admin/python-packages", headers=admin).json()
        row = next((p for p in inv["packages"] if p["name"] == name and p["source"] == "admin"), None)
        if row is None:
            return {"gone": True, "inventory": inv}
        if row["status"] in ("installed", "failed"):
            return row
        time.sleep(0.1)
    raise AssertionError(f"package {name} never reached a terminal state")


def _wait_idle(client, admin, timeout_s: float = 10.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        inv = client.get("/api/admin/python-packages", headers=admin).json()
        if not inv["environment"]["busy"]:
            return
        time.sleep(0.1)
    raise AssertionError("package job never went idle")


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def test_inventory_lists_bundled_set_with_versions(client, admin):
    r = client.get("/api/admin/python-packages", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    bundled = {p["name"]: p for p in body["packages"] if p["source"] == "bundled"}
    assert set(bundled) == set(pkg_service.BUNDLED_PACKAGES)
    # Dev venv has the [server-fns] extra installed — real versions resolve.
    assert bundled["pandas"]["version"], "bundled pandas version missing"
    assert all(p["status"] == "installed" for p in bundled.values())
    env = body["environment"]
    assert env["pip_available"] is True
    assert env["managed_dir"] == str(_MANAGED)
    assert env["busy"] is False


def test_non_admin_is_denied_on_all_routes(client):
    tok = client.post("/api/auth/login", json={"username": "developer", "password": "password"}).json()["access_token"]
    dev = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/admin/python-packages", headers=dev).status_code == 403
    assert client.get("/api/admin/python-packages/lookup?name=x", headers=dev).status_code == 403
    assert client.post("/api/admin/python-packages", json={"name": "x"}, headers=dev).status_code == 403
    assert client.delete("/api/admin/python-packages/x", headers=dev).status_code == 403
    assert client.post("/api/admin/python-packages/rebuild", headers=dev).status_code == 403


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def test_install_happy_path(client, admin, fake_pip):
    r = client.post("/api/admin/python-packages", json={"name": "tabulate"}, headers=admin)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "pending"

    row = _poll_terminal(client, admin, "tabulate")
    assert row["status"] == "installed", row
    assert row["version"] == "1.0.0"
    assert (_MANAGED / "tabulate-1.0.0.dist-info").is_dir()

    import sqlite3
    from src.config import settings
    db_path = settings.database_url.split("///", 1)[1]
    con = sqlite3.connect(db_path)
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='python_package.install' "
            "AND resource_id='tabulate'").fetchone()[0]
    finally:
        con.close()
    assert n >= 1


def test_pinned_install_reconstructs_the_spec(client, admin, fake_pip):
    r = client.post("/api/admin/python-packages",
                    json={"name": "Foo_Bar", "version": "2.5.1"}, headers=admin)
    assert r.status_code == 202, r.text
    row = _poll_terminal(client, admin, "foo-bar")
    assert row["status"] == "installed"
    args = fake_pip["calls"][-1]
    assert "Foo_Bar==2.5.1" in args          # reconstructed spec, display name kept
    assert "--only-binary=:all:" in args
    assert "--upgrade" in args
    assert "--target" in args
    assert "--index-url" not in args         # no index configured


def test_index_url_setting_reaches_pip_args(client, admin, fake_pip):
    assert client.put("/api/admin/settings",
                      json={"pip_index_url": "https://mirror.corp/simple"},
                      headers=admin).status_code == 200
    try:
        client.post("/api/admin/python-packages", json={"name": "idxpkg"}, headers=admin)
        _poll_terminal(client, admin, "idxpkg")
        args = fake_pip["calls"][-1]
        assert "--index-url" in args
        assert args[args.index("--index-url") + 1] == "https://mirror.corp/simple"
    finally:
        client.put("/api/admin/settings", json={"pip_index_url": ""}, headers=admin)


def test_invalid_names_and_versions_are_rejected(client, admin, fake_pip):
    for bad in ("pkg @ http://evil", "-e .", "..\\evil", "a b", "", "pkg;rm"):
        r = client.post("/api/admin/python-packages", json={"name": bad}, headers=admin)
        assert r.status_code in (400, 422), f"{bad!r}: {r.status_code}"
    r = client.post("/api/admin/python-packages",
                    json={"name": "pkg", "version": ">=1.0"}, headers=admin)
    assert r.status_code == 400
    assert "version" in r.json()["detail"].lower()
    assert not fake_pip["calls"], "invalid input must never reach pip"


def test_concurrent_operation_is_a_409(client, admin, fake_pip):
    fake_pip["block"] = threading.Event()
    try:
        assert client.post("/api/admin/python-packages", json={"name": "slowpkg"},
                           headers=admin).status_code == 202
        r = client.post("/api/admin/python-packages", json={"name": "otherpkg"}, headers=admin)
        assert r.status_code == 409
        assert "already running" in r.json()["detail"]
        inv = client.get("/api/admin/python-packages", headers=admin).json()
        assert inv["environment"]["busy"] is True
    finally:
        fake_pip["block"].set()
    _poll_terminal(client, admin, "slowpkg")


def test_failed_install_surfaces_pip_output(client, admin, fake_pip):
    fake_pip["rc"] = 1
    fake_pip["out"] = "ERROR: No matching distribution found for ghostpkg==99.0"
    client.post("/api/admin/python-packages",
                json={"name": "ghostpkg", "version": "99.0"}, headers=admin)
    row = _poll_terminal(client, admin, "ghostpkg")
    assert row["status"] == "failed"
    assert "No matching distribution" in row["error"]


def test_reinstall_same_name_different_case_reuses_the_row(client, admin, fake_pip):
    client.post("/api/admin/python-packages", json={"name": "case-pkg"}, headers=admin)
    _poll_terminal(client, admin, "case-pkg")
    client.post("/api/admin/python-packages", json={"name": "Case_Pkg", "version": "3.0"}, headers=admin)
    row = _poll_terminal(client, admin, "case-pkg")
    assert row["status"] == "installed"
    inv = client.get("/api/admin/python-packages", headers=admin).json()
    rows = [p for p in inv["packages"] if p["name"] == "case-pkg" and p["source"] == "admin"]
    assert len(rows) == 1
    assert rows[0]["pinned_version"] == "3.0"


def test_pip_unavailable_is_a_fixable_503(client, admin, monkeypatch):
    monkeypatch.setattr(python_env, "pip_cmd", lambda: None)
    monkeypatch.setattr(pkg_service.python_env, "pip_cmd", lambda: None)
    r = client.post("/api/admin/python-packages", json={"name": "whatever"}, headers=admin)
    assert r.status_code == 503
    assert "installer" in r.json()["detail"].lower()
    inv = client.get("/api/admin/python-packages", headers=admin).json()
    assert inv["environment"]["pip_available"] is False


# ---------------------------------------------------------------------------
# Uninstall + rebuild
# ---------------------------------------------------------------------------

def test_uninstall_rebuilds_without_the_package(client, admin, fake_pip):
    client.post("/api/admin/python-packages", json={"name": "keepme"}, headers=admin)
    _poll_terminal(client, admin, "keepme")
    client.post("/api/admin/python-packages", json={"name": "dropme"}, headers=admin)
    _poll_terminal(client, admin, "dropme")

    r = client.delete("/api/admin/python-packages/dropme", headers=admin)
    assert r.status_code == 202, r.text
    _wait_idle(client, admin)

    inv = client.get("/api/admin/python-packages", headers=admin).json()
    names = [p["name"] for p in inv["packages"] if p["source"] == "admin"]
    assert "dropme" not in names
    assert "keepme" in names
    # The rebuilt dir contains only the remaining manifest.
    assert not (_MANAGED / "dropme-1.0.0.dist-info").exists()
    assert (_MANAGED / "keepme-1.0.0.dist-info").is_dir()
    # Rebuild ran ONE pip install with the remaining specs.
    rebuild_args = fake_pip["calls"][-1]
    assert "keepme" in rebuild_args and "dropme" not in rebuild_args


def test_uninstall_bundled_is_refused(client, admin, fake_pip):
    r = client.delete("/api/admin/python-packages/pandas", headers=admin)
    assert r.status_code == 400
    assert "bundled" in r.json()["detail"].lower()


def test_uninstall_unknown_is_404(client, admin, fake_pip):
    assert client.delete("/api/admin/python-packages/never-heard-of-it",
                         headers=admin).status_code == 404


def test_manual_rebuild_endpoint(client, admin, fake_pip):
    r = client.post("/api/admin/python-packages/rebuild", headers=admin)
    assert r.status_code == 202, r.text
    _wait_idle(client, admin)
    assert any("install" in c for c in fake_pip["calls"][-1])


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def test_stuck_transient_rows_are_reconciled(client, admin):
    from src.database import async_session
    from src.python_packages.models import PythonPackage

    async def _insert():
        async with async_session() as db:
            db.add(PythonPackage(name="stuckpkg", requested_spec="stuckpkg",
                                 status="installing"))
            await db.commit()

    asyncio.run(_insert())
    inv = client.get("/api/admin/python-packages", headers=admin).json()
    row = next(p for p in inv["packages"] if p["name"] == "stuckpkg")
    assert row["status"] == "failed"
    assert "restart" in row["error"].lower()


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)


def test_lookup_pypi_json(client, admin, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/pypi/tabulate/json"
        return httpx.Response(200, json={
            "info": {"name": "tabulate", "summary": "Pretty-print tabular data"},
            "releases": {
                "0.9.0": [{"yanked": False}],
                "0.8.10": [{"yanked": False}],
                "0.8.11": [{"yanked": True}],   # fully yanked → filtered
            },
        })

    monkeypatch.setattr(pkg_service, "_make_client", lambda: _mock_client(handler))
    r = client.get("/api/admin/python-packages/lookup?name=tabulate", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["latest"] == "0.9.0"
    assert body["versions"] == ["0.9.0", "0.8.10"]
    assert "Pretty-print" in body["summary"]


def test_lookup_pep691_index(client, admin, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/tabulate/")
        return httpx.Response(200, json={
            "name": "tabulate",
            "files": [
                {"filename": "tabulate-0.9.0-py3-none-any.whl"},
                {"filename": "tabulate-0.8.10.tar.gz"},
            ],
        }, headers={"content-type": "application/vnd.pypi.simple.v1+json"})

    monkeypatch.setattr(pkg_service, "_make_client", lambda: _mock_client(handler))
    assert client.put("/api/admin/settings",
                      json={"pip_index_url": "https://mirror.corp/simple"},
                      headers=admin).status_code == 200
    try:
        r = client.get("/api/admin/python-packages/lookup?name=tabulate", headers=admin)
        body = r.json()
        assert body["available"] is True
        assert body["versions"] == ["0.9.0", "0.8.10"]
    finally:
        client.put("/api/admin/settings", json={"pip_index_url": ""}, headers=admin)


def test_lookup_html_index_degrades_gracefully(client, admin, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><a>tabulate-0.9.0.tar.gz</a></html>",
                              headers={"content-type": "text/html"})

    monkeypatch.setattr(pkg_service, "_make_client", lambda: _mock_client(handler))
    assert client.put("/api/admin/settings",
                      json={"pip_index_url": "https://old.mirror/simple"},
                      headers=admin).status_code == 200
    try:
        body = client.get("/api/admin/python-packages/lookup?name=tabulate",
                          headers=admin).json()
        assert body["available"] is False
        assert "PEP 691" in body["error"]
    finally:
        client.put("/api/admin/settings", json={"pip_index_url": ""}, headers=admin)


def test_lookup_network_error_degrades_gracefully(client, admin, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(pkg_service, "_make_client", lambda: _mock_client(handler))
    body = client.get("/api/admin/python-packages/lookup?name=tabulate", headers=admin).json()
    assert body["available"] is False
    assert "lookup failed" in body["error"].lower()


def test_lookup_invalid_name_is_400(client, admin):
    assert client.get("/api/admin/python-packages/lookup?name=..%5Cevil",
                      headers=admin).status_code == 400
