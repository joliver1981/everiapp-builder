"""One-off: boot an app's dev server and capture marketplace screenshots.

Usage: python scripts/capture-screenshot.py <app_id> <out_prefix>
Writes <out_prefix>-1.png (desktop) and <out_prefix>-2.png (narrow).
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8800"
app_id, out_prefix = sys.argv[1], sys.argv[2]


def api(path, method="GET", body=None, token=None):
    req = urllib.request.Request(f"{BASE}{path}", method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=30) as r:
        return json.loads(r.read())


token = api("/api/auth/login", "POST", {"username": "admin", "password": "password"})["access_token"]

api(f"/api/apps/{app_id}/runtime/start", "POST", {"source": "draft"}, token)
port = None
for _ in range(60):
    st = api(f"/api/apps/{app_id}/runtime/status", token=token)
    if st["status"] == "running" and st.get("port"):
        port = st["port"]
        break
    if st["status"] == "error":
        sys.exit(f"runtime error: {st.get('error')}")
    time.sleep(2)
if not port:
    sys.exit("app never reached running state")

url = f"http://127.0.0.1:{port}/apps/{app_id}/"
print(f"app running at {url}")

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(url, wait_until="networkidle", timeout=60_000)
    page.wait_for_timeout(1500)  # let fonts/transitions settle
    page.screenshot(path=f"{out_prefix}-1.png")
    page.set_viewport_size({"width": 480, "height": 800})
    page.wait_for_timeout(500)
    page.screenshot(path=f"{out_prefix}-2.png")
    browser.close()

api(f"/api/apps/{app_id}/runtime/stop", "POST", {}, token)
print("screenshots captured, runtime stopped")
