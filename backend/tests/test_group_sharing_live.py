"""Live end-to-end: builder (in-process) <-> marketplace (real HTTP).

Group Sharing spans two codebases, and the unit suites on each side only
prove their own half. This module runs the whole flow against a real
marketplace dev server: accounts are seeded with the marketplace's
`scripts/seed-dev.mjs`, group management goes through NextAuth's real
credentials login and the real /api/groups routes, and the builder side runs
in-process through FastAPI's TestClient with its real HTTP client pointed at
the marketplace.

Opt-in. Runs only when a marketplace is reachable at
AIHUB_E2E_MARKETPLACE_URL (default http://localhost:3000) and the marketplace
repo (AIHUB_E2E_MARKETPLACE_DIR, default C:/src/aihub-marketplace) is present
so accounts can be seeded. Skipped otherwise, so the green-gate stays fast on
machines without the marketplace running.

Everything it creates is uniquely suffixed and left PRIVATE (or deleted), so
the public catalog is never polluted.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_group_sharing_live.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_group_sharing_live")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "group-sharing-live-test")

MP_URL = os.environ.get("AIHUB_E2E_MARKETPLACE_URL", "http://localhost:3000").rstrip("/")
MP_DIR = Path(os.environ.get("AIHUB_E2E_MARKETPLACE_DIR", "C:/src/aihub-marketplace"))
NODE = os.environ.get("AIHUB_E2E_NODE", r"C:\Program Files\nodejs\node.exe")
if not Path(NODE).exists():
    NODE = "node"


def _marketplace_up() -> bool:
    try:
        return httpx.get(f"{MP_URL}/api/apps?limit=1", timeout=5).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not (MP_DIR / "scripts" / "seed-dev.mjs").exists() or not _marketplace_up(),
    reason=f"needs a marketplace dev server at {MP_URL} and the repo at {MP_DIR}",
)

from fastapi.testclient import TestClient  # noqa: E402
from src.config import settings  # noqa: E402
from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402

RUN = f"{int(time.time())}"[-7:]


# ---------------------------------------------------------------- marketplace helpers

class Persona:
    """A seeded marketplace developer account: API key + a logged-in session."""

    def __init__(self, tag: str, xff: str):
        self.email = f"e2e-{tag}-{RUN}@example.com"
        self.password = "E2ePassw0rd!"
        self.tag = tag
        self.xff = xff  # spreads the marketplace's per-IP rate limit across personas
        # The seeder reads the marketplace's own .env.local; it must not
        # inherit this test process's builder DATABASE_URL (dotenv never
        # overrides an existing variable).
        env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        out = subprocess.run(
            [NODE, "scripts/seed-dev.mjs", self.email, self.password, f"E2E {tag.title()} {RUN}"],
            cwd=str(MP_DIR), capture_output=True, text=True, timeout=120, env=env,
            encoding="utf-8", errors="replace",  # dotenv's banner has emoji; cp1252 would choke
        )
        assert out.returncode == 0, f"seed-dev failed for {tag}: {out.stderr[-500:]}"
        m = re.search(r"API key \(shown once\): (aihub_[0-9a-f]+)", out.stdout)
        assert m, f"no API key in seed output: {out.stdout}"
        self.api_key = m.group(1)
        self.session = self._login()
        self.user_id: str | None = None

    def _login(self) -> httpx.Client:
        c = httpx.Client(base_url=MP_URL, follow_redirects=False, timeout=60,
                         headers={"X-Forwarded-For": self.xff})
        csrf = c.get("/api/auth/csrf").json()["csrfToken"]
        r = c.post(
            "/api/auth/callback/credentials",
            data={"csrfToken": csrf, "email": self.email, "password": self.password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert any("session-token" in k for k in c.cookies.keys()), \
            f"marketplace login failed for {self.tag}: {r.status_code} {r.text[:300]}"
        return c

    @property
    def key_headers(self) -> dict:
        return {"X-API-Key": self.api_key, "X-Forwarded-For": self.xff}


def anon() -> httpx.Client:
    return httpx.Client(base_url=MP_URL, timeout=60, headers={"X-Forwarded-For": "10.99.0.250"})


def mp_slugs(client: httpx.Client, q: str, headers: dict | None = None) -> list[str]:
    r = client.get("/api/apps", params={"q": q, "limit": 50}, headers=headers or {})
    assert r.status_code == 200, r.text
    return sorted(a["slug"] for a in r.json()["apps"])


def mp_app(client: httpx.Client, q: str, slug: str, headers: dict | None = None) -> dict | None:
    r = client.get("/api/apps", params={"q": q, "limit": 50}, headers=headers or {})
    assert r.status_code == 200, r.text
    return next((a for a in r.json()["apps"] if a["slug"] == slug), None)


# ---------------------------------------------------------------- builder helpers

@pytest.fixture(scope="module")
def builder():
    asyncio.run(init_db())
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "password"})
        assert r.status_code == 200, r.text
        c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        yield c


def configure_builder(builder: TestClient, api_key: str) -> None:
    r = builder.put("/api/admin/settings", json={
        "marketplace_url": MP_URL, "marketplace_api_key": api_key,
    })
    assert r.status_code == 200, r.text


def new_builder_app(builder: TestClient, name: str) -> str:
    r = builder.post("/api/apps", json={"name": name, "description": f"{name} — e2e"})
    assert r.status_code == 201, r.text
    app_id = r.json()["id"]
    draft = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
    (draft / "src").mkdir(parents=True, exist_ok=True)
    (draft / "src" / "e2e.ts").write_text(f"export const E2E = '{RUN}'\n")
    r = builder.post(f"/api/apps/{app_id}/versions", json={"notes": "v1"})
    assert r.status_code == 201, r.text
    return app_id


def builder_publish(builder: TestClient, app_id: str, semver: str, **audience) -> dict:
    r = builder.post("/api/marketplace/publish-external", json={
        "app_id": app_id,
        "short_description": "End-to-end group sharing check.",
        "description": "# E2E\n\nThis listing exists only to verify group sharing end to end.",
        "version_semver": semver,
        "capture_screenshots": False,
        **audience,
    })
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- the flow

@pytest.fixture(scope="module")
def world():
    """Accounts A (publisher), B (member), C (outsider); A owns a group; B joins by link."""
    a = Persona("alice", "10.99.0.1")
    b = Persona("bob", "10.99.0.2")
    c = Persona("carol", "10.99.0.3")

    r = a.session.post("/api/groups", json={"name": f"E2E Roundtable {RUN}"})
    assert r.status_code == 201, r.text
    group = r.json()["group"]

    r = a.session.post(f"/api/groups/{group['slug']}/invites", json={})
    assert r.status_code == 201, r.text
    invite_url = r.json()["invite"]["inviteUrl"]
    token = invite_url.rsplit("/", 1)[-1]

    r = b.session.post(f"/api/groups/invites/{token}")
    assert r.status_code == 200, r.text
    assert r.json()["alreadyMember"] is False

    r = a.session.get(f"/api/groups/{group['slug']}")
    assert r.status_code == 200, r.text
    for m in r.json()["members"]:
        if m["email"] == b.email:
            b.user_id = m["userId"]
        if m["email"] == a.email:
            a.user_id = m["userId"]
    assert b.user_id and a.user_id

    yield {"a": a, "b": b, "c": c, "group": group, "token": token}

    # Teardown: the group goes away; anything still shared reverts to
    # private-with-no-shares, so nothing public is left behind.
    a.session.delete(f"/api/groups/{group['slug']}")


def test_01_publish_private_to_group_from_builder(builder, world):
    a, group = world["a"], world["group"]
    configure_builder(builder, a.api_key)
    app_id = new_builder_app(builder, f"E2E Shared {RUN}")
    world["app_id"] = app_id

    result = builder_publish(builder, app_id, "1.0.0",
                             visibility="private", share_to_groups=[group["slug"]])
    world["slug"] = result["slug"]
    assert result["visibility"] == "private"
    assert [g["slug"] for g in result["shared_with"]] == [group["slug"]]
    assert result["audience"].startswith("Private")

    # Sticky audience for the next publish dialog.
    listing = builder.get(f"/api/apps/{app_id}").json()["marketplace_listing"]
    assert listing["visibility"] == "private"
    assert listing["share_to_groups"] == [group["slug"]]


def test_02_outsiders_get_nothing(world):
    slug, c = world["slug"], world["c"]
    q = f"E2E Shared {RUN}"
    with anon() as an:
        assert slug not in mp_slugs(an, q)
        assert an.get(f"/api/apps/{slug}/versions").status_code == 404
        assert an.get(f"/api/apps/{slug}/download").status_code == 404
        assert an.get(f"/api/apps/{slug}/reviews").status_code == 404
        feed = an.get("/api/feed", params={"q": q, "limit": 50}).json()["apps"]
        assert slug not in [x["slug"] for x in feed]
        # The detail page 404s too (server-rendered).
        assert an.get(f"/apps/{slug}").status_code == 404
    with anon() as outsider:
        assert slug not in mp_slugs(outsider, q, c.key_headers)
        assert outsider.get(f"/api/apps/{slug}/versions", headers=c.key_headers).status_code == 404
        assert outsider.get(f"/api/apps/{slug}/download", headers=c.key_headers).status_code == 404


def test_03_member_sees_it_and_the_download_is_audited(world):
    slug, b, group = world["slug"], world["b"], world["group"]
    q = f"E2E Shared {RUN}"
    with anon() as member:
        card = mp_app(member, q, slug, b.key_headers)
        assert card is not None
        assert card["visibility"] == "private"
        assert [g["slug"] for g in card["sharedGroups"]] == [group["slug"]]
        r = member.get(f"/api/apps/{slug}/versions", headers=b.key_headers)
        assert r.status_code == 200
        assert [v["version"] for v in r.json()["versions"]] == ["1.0.0"]
        r = member.get(f"/api/apps/{slug}/download", headers=b.key_headers)
        assert r.status_code in (200, 302), r.text[:200]
    # The group page lists it as shared.
    r = b.session.get(f"/api/groups/{group['slug']}")
    assert r.status_code == 200
    assert slug in [x["slug"] for x in r.json()["apps"]]


def test_04_member_installs_through_the_builder(builder, world):
    b, slug = world["b"], world["slug"]
    configure_builder(builder, b.api_key)
    r = builder.get("/api/marketplace/remote", params={"q": f"E2E Shared {RUN}"})
    assert r.status_code == 200, r.text
    card = next((x for x in r.json()["apps"] if x["slug"] == slug), None)
    assert card is not None and card["visibility"] == "private"

    r = builder.post("/api/marketplace/remote/install", json={"slug": slug})
    assert r.status_code == 200, r.text
    installed = builder.get(f"/api/apps/{r.json()['app_id']}").json()
    assert installed["installed_from"] == f"marketplace:{slug}"
    world["installed_id"] = r.json()["app_id"]

    # An outsider's builder can't even see it, let alone install it.
    configure_builder(builder, world["c"].api_key)
    r = builder.get("/api/marketplace/remote", params={"q": f"E2E Shared {RUN}"})
    assert slug not in [x["slug"] for x in r.json()["apps"]]
    r = builder.post("/api/marketplace/remote/install", json={"slug": slug})
    assert r.status_code == 400


def test_05_old_client_republish_keeps_it_private(world):
    """A v0.20.0-shaped publish (no audience fields) must never flip the listing."""
    a, slug = world["a"], world["slug"]
    with anon() as legacy:
        r = legacy.post("/api/publish", headers=a.key_headers, json={
            "name": f"E2E Shared {RUN}", "slug": slug,
            "shortDescription": "End-to-end group sharing check.",
            "description": "# E2E\n\nRe-published by a client that predates visibility.",
            "category": "general", "version": "1.0.1",
        })
        assert r.status_code == 200, r.text
        assert r.json()["app"]["visibility"] == "private"
        assert [g["slug"] for g in r.json()["app"]["sharedWith"]] == [world["group"]["slug"]]
    with anon() as an:
        assert slug not in mp_slugs(an, f"E2E Shared {RUN}")


def test_06_owner_flips_visibility_from_the_dashboard(world):
    a, slug = world["a"], world["slug"]
    q = f"E2E Shared {RUN}"
    r = a.session.patch(f"/api/developer/apps/{slug}/visibility", json={"visibility": "public"})
    assert r.status_code == 200, r.text
    assert r.json()["visibility"] == "public"
    with anon() as an:
        assert slug in mp_slugs(an, q)
    r = a.session.patch(f"/api/developer/apps/{slug}/visibility",
                        json={"visibility": "private", "shareToGroups": [world["group"]["slug"]]})
    assert r.status_code == 200, r.text
    with anon() as an:
        assert slug not in mp_slugs(an, q)


def test_07_only_the_owner_invites(world):
    b, group = world["b"], world["group"]
    r = b.session.post(f"/api/groups/{group['slug']}/invites", json={})
    assert r.status_code == 403


def test_08_removed_member_loses_access_immediately(world):
    a, b, slug, group = world["a"], world["b"], world["slug"], world["group"]
    r = a.session.delete(f"/api/groups/{group['slug']}/members/{b.user_id}")
    assert r.status_code == 200, r.text
    with anon() as former:
        assert former.get(f"/api/apps/{slug}/versions", headers=b.key_headers).status_code == 404
        assert slug not in mp_slugs(former, f"E2E Shared {RUN}", b.key_headers)
    assert b.session.get(f"/api/groups/{group['slug']}").status_code == 404


def test_09_deleting_the_group_leaves_the_app_private(builder, world):
    a, b, c, slug, group = world["a"], world["b"], world["c"], world["slug"], world["group"]
    q = f"E2E Shared {RUN}"
    r = a.session.delete(f"/api/groups/{group['slug']}")
    assert r.status_code == 200, r.text
    with anon() as x:
        assert slug not in mp_slugs(x, q)
        assert slug not in mp_slugs(x, q, b.key_headers)
        assert slug not in mp_slugs(x, q, c.key_headers)
        assert slug in mp_slugs(x, q, a.key_headers)  # publisher always sees their own


def test_10_new_public_app_defaults_public_then_is_tidied_away(builder, world):
    a = world["a"]
    configure_builder(builder, a.api_key)
    app_id = new_builder_app(builder, f"E2E Public {RUN}")
    result = builder_publish(builder, app_id, "1.0.0")  # no audience chosen
    assert result["visibility"] == "public"
    with anon() as an:
        assert result["slug"] in mp_slugs(an, f"E2E Public {RUN}")
    # Leave nothing public behind.
    r = a.session.patch(f"/api/developer/apps/{result['slug']}/visibility", json={"visibility": "private"})
    assert r.status_code == 200, r.text
    with anon() as an:
        assert result["slug"] not in mp_slugs(an, f"E2E Public {RUN}")
