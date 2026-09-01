"""Builder-chat attachments (screenshots, images, PDFs, text files) — via the
real HTTP upload route + the real chat WebSocket.

Flow under test: POST /api/ai/attachments (multipart) → ids → WS chat payload
`attachment_ids` → the LLM call receives OpenAI-format multimodal content
(`image_url` / `file` parts, text files inlined) → the uploads are bound to
the saved user message → GET /api/ai/conversations/{app} lists them → GET
/api/ai/attachments/{id} serves the bytes back for thumbnails.
"""
import base64
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.ai import router as ai_router_mod

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
PDF_MIN = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
           b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
TXT = b"hello from the attached notes file\nline two\n"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def developer_token(client: TestClient) -> str:
    r = client.post("/api/auth/login", json={"username": "developer", "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def _mk_provider(client, token, provider_type="anthropic", model="claude-fable-5") -> str:
    r = client.post("/api/admin/ai-providers", headers=_auth(token), json={
        "name": f"prov-{uuid.uuid4().hex[:6]}",
        "provider_type": provider_type,
        "api_key": "sk-test-not-real",
        "default_model": model,
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _mk_app(client, token) -> str:
    r = client.post("/api/apps", json={"name": f"att-{uuid.uuid4().hex[:6]}"}, headers=_auth(token))
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _upload(client, token, app_id, files):
    """files: [(name, bytes, mime)] → response"""
    return client.post(
        "/api/ai/attachments", headers=_auth(token),
        data={"app_id": app_id},
        files=[("files", (name, data, mime)) for name, data, mime in files],
    )


def _mk_chunk(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None)


@pytest.fixture()
def capture_llm(monkeypatch):
    """Stub the LLM boundary and capture every call's kwargs."""
    calls: list[dict] = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)

        async def gen():
            yield _mk_chunk("Looked at it. ")
            yield _mk_chunk("No file changes.")
        return gen()

    monkeypatch.setattr("src.ai.service.acompletion", fake_acompletion)
    return calls


def _chat(client, token, app_id, provider_id, message, attachment_ids=None, conversation_id=None):
    ai_router_mod._rate_buckets.clear()  # per-user limiter is process-global across test modules
    received = []
    with client.websocket_connect("/api/ai/chat") as ws:
        ws.send_json({"token": token})
        assert ws.receive_json()["type"] == "authenticated"
        payload = {"app_id": app_id, "message": message, "provider_id": provider_id}
        if attachment_ids is not None:
            payload["attachment_ids"] = attachment_ids
        if conversation_id:
            payload["conversation_id"] = conversation_id
        ws.send_json(payload)
        for _ in range(80):
            msg = ws.receive_json()
            received.append(msg)
            if msg["type"] in ("done", "error"):
                break
    return received


def _last_user_content(calls):
    msgs = calls[-1]["messages"]
    users = [m for m in msgs if m["role"] == "user"]
    return users[-1]["content"]


# --- upload route -------------------------------------------------------------

def test_upload_classifies_and_returns_ids(client, admin_token):
    app_id = _mk_app(client, admin_token)
    r = _upload(client, admin_token, app_id, [
        ("shot.png", PNG_1x1, "image/png"),
        ("spec.pdf", PDF_MIN, "application/pdf"),
        ("notes.txt", TXT, "text/plain"),
        # Browsers often send code files as octet-stream — the extension decides.
        ("Widget.tsx", b"export const x = 1\n", "application/octet-stream"),
    ])
    assert r.status_code == 200, r.text
    atts = r.json()["attachments"]
    assert [a["kind"] for a in atts] == ["image", "pdf", "text", "text"]
    assert [a["name"] for a in atts] == ["shot.png", "spec.pdf", "notes.txt", "Widget.tsx"]
    assert atts[0]["mime"] == "image/png" and atts[0]["size"] == len(PNG_1x1)
    assert atts[3]["mime"] == "text/plain"
    assert all(a["id"] for a in atts)

    # Bytes round-trip with the stored MIME.
    g = client.get(f"/api/ai/attachments/{atts[0]['id']}", headers=_auth(admin_token))
    assert g.status_code == 200
    assert g.headers["content-type"].startswith("image/png")
    assert g.content == PNG_1x1


def test_upload_refuses_unsupported_type_loudly(client, admin_token):
    app_id = _mk_app(client, admin_token)
    r = _upload(client, admin_token, app_id, [("tool.exe", b"MZ\x90\x00", "application/octet-stream")])
    assert r.status_code == 415, r.text
    assert "not a supported attachment type" in r.json()["detail"]
    assert "PDF" in r.json()["detail"]


def test_upload_requires_existing_app_and_auth(client, admin_token):
    r = _upload(client, admin_token, "nope-" + uuid.uuid4().hex, [("a.png", PNG_1x1, "image/png")])
    assert r.status_code == 404
    r = client.post("/api/ai/attachments", data={"app_id": "x"},
                    files=[("files", ("a.png", PNG_1x1, "image/png"))])
    assert r.status_code in (401, 403)


# --- chat turn ----------------------------------------------------------------

def test_chat_turn_sends_multimodal_parts_and_binds_uploads(client, admin_token, capture_llm):
    provider_id = _mk_provider(client, admin_token)
    app_id = _mk_app(client, admin_token)
    atts = _upload(client, admin_token, app_id, [
        ("shot.png", PNG_1x1, "image/png"),
        ("spec.pdf", PDF_MIN, "application/pdf"),
        ("notes.txt", TXT, "text/plain"),
    ]).json()["attachments"]
    ids = [a["id"] for a in atts]

    received = _chat(client, admin_token, app_id, provider_id, "Make it look like this", ids)
    assert any(m["type"] == "done" for m in received), received
    assert capture_llm, "LLM was not called"

    content = _last_user_content(capture_llm)
    assert isinstance(content, list), content
    assert content[0]["type"] == "text"
    text = content[0]["text"]
    assert text.startswith("Make it look like this")
    # Text files are inlined into the text part (works for text-only models too).
    assert "[Attached file: notes.txt]" in text and "hello from the attached notes file" in text
    # Image → OpenAI-format image_url data URL; PDF → file part (litellm maps
    # both to Anthropic image/document blocks).
    img = [p for p in content if p["type"] == "image_url"]
    pdf = [p for p in content if p["type"] == "file"]
    assert len(img) == 1 and len(pdf) == 1
    assert img[0]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG_1x1).decode()
    assert pdf[0]["file"]["filename"] == "spec.pdf"
    assert pdf[0]["file"]["file_data"].startswith("data:application/pdf;base64,")
    # Messages WITHOUT attachments keep plain-string content (no behaviour change).
    assert all(isinstance(m["content"], str) for m in capture_llm[-1]["messages"] if m["role"] == "system")

    # History lists the attachments (metadata only) on the user message.
    h = client.get(f"/api/ai/conversations/{app_id}", headers=_auth(admin_token)).json()
    user_msgs = [m for m in h["messages"] if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert [a["name"] for a in user_msgs[0]["attachments"]] == ["shot.png", "spec.pdf", "notes.txt"]
    assert user_msgs[0]["attachments"][0]["kind"] == "image"
    assert "data" not in user_msgs[0]["attachments"][0]
    assistant = [m for m in h["messages"] if m["role"] == "assistant"]
    assert assistant and assistant[0]["attachments"] == []

    # A follow-up turn (no new attachments) still replays the screenshot from
    # the earlier turn — it is inside the history window.
    received = _chat(client, admin_token, app_id, provider_id, "now add a chart",
                     conversation_id=h["conversation_id"])
    assert any(m["type"] == "done" for m in received), received
    msgs = capture_llm[-1]["messages"]
    users = [m for m in msgs if m["role"] == "user"]
    assert isinstance(users[-1]["content"], str) and users[-1]["content"] == "now add a chart"
    assert isinstance(users[-2]["content"], list)
    assert any(p["type"] == "image_url" for p in users[-2]["content"])

    # Once bound, the ids can't be attached to another message.
    received = _chat(client, admin_token, app_id, provider_id, "again", ids)
    assert received[-1]["type"] == "error" and "could not be found" in received[-1]["data"]


def test_attachments_only_message_is_allowed(client, admin_token, capture_llm):
    provider_id = _mk_provider(client, admin_token)
    app_id = _mk_app(client, admin_token)
    atts = _upload(client, admin_token, app_id, [("bug.png", PNG_1x1, "image/png")]).json()["attachments"]
    received = _chat(client, admin_token, app_id, provider_id, "", [atts[0]["id"]])
    assert any(m["type"] == "done" for m in received), received
    content = _last_user_content(capture_llm)
    assert content[0] == {"type": "text", "text": "(see attached)"}
    assert content[1]["type"] == "image_url"
    h = client.get(f"/api/ai/conversations/{app_id}", headers=_auth(admin_token)).json()
    assert h["messages"][0]["attachments"][0]["name"] == "bug.png"

    # Truly empty (no text, no attachments) is still refused.
    received = _chat(client, admin_token, app_id, provider_id, "", [])
    assert received[-1]["type"] == "error"


def test_another_users_upload_cannot_be_attached(client, admin_token, developer_token, capture_llm):
    provider_id = _mk_provider(client, admin_token)
    app_id = _mk_app(client, admin_token)
    atts = _upload(client, developer_token, app_id, [("theirs.png", PNG_1x1, "image/png")]).json()["attachments"]
    received = _chat(client, admin_token, app_id, provider_id, "use this", [atts[0]["id"]])
    assert received[-1]["type"] == "error" and "could not be found" in received[-1]["data"]
    assert not capture_llm


def test_text_only_model_refuses_images_before_spending(client, admin_token, capture_llm):
    # gpt-3.5-turbo is in litellm's registry WITHOUT vision support.
    provider_id = _mk_provider(client, admin_token, provider_type="openai", model="gpt-3.5-turbo")
    app_id = _mk_app(client, admin_token)
    atts = _upload(client, admin_token, app_id, [("shot.png", PNG_1x1, "image/png")]).json()["attachments"]
    received = _chat(client, admin_token, app_id, provider_id, "look", [atts[0]["id"]])
    assert received[-1]["type"] == "error", received
    assert "does not accept images" in received[-1]["data"]
    assert not capture_llm, "must refuse before calling the model"
    # Nothing persisted: the user can switch provider and resend the same upload.
    h = client.get(f"/api/ai/conversations/{app_id}", headers=_auth(admin_token)).json()
    assert h["messages"] == []
    # Text files are fine on a text-only model (inlined).
    t = _upload(client, admin_token, app_id, [("n.txt", TXT, "text/plain")]).json()["attachments"]
    received = _chat(client, admin_token, app_id, provider_id, "read", [t[0]["id"]])
    assert any(m["type"] == "done" for m in received), received
    assert "hello from the attached notes file" in _last_user_content(capture_llm)


def test_providers_list_carries_capability_hints(client, admin_token):
    _mk_provider(client, admin_token, provider_type="openai", model="gpt-3.5-turbo")
    _mk_provider(client, admin_token, provider_type="anthropic", model="claude-brand-new-99")
    r = client.get("/api/ai/providers", headers=_auth(admin_token))
    assert r.status_code == 200
    by_model = {p["default_model"]: p for p in r.json()}
    assert by_model["gpt-3.5-turbo"]["supports_vision"] is False
    # Unknown Anthropic id: assume multimodal rather than refuse.
    assert by_model["claude-brand-new-99"]["supports_vision"] is True


# --- pure helpers -------------------------------------------------------------

def test_model_capabilities_matrix():
    from src.llm_compat import model_capabilities
    assert model_capabilities("anthropic", "claude-opus-4-1")["vision"] is True
    assert model_capabilities("openai", "gpt-4o")["pdf"] is True
    assert model_capabilities("openai", "gpt-3.5-turbo") == {"vision": False, "pdf": False, "known": True}
    unknown = model_capabilities("anthropic", "claude-unknown-99")
    assert unknown["known"] is False and unknown["vision"] is True
    custom = model_capabilities("ollama", "llama-whatever")
    assert custom == {"vision": None, "pdf": None, "known": False}
    assert model_capabilities(None, None)["known"] is False


def test_classify_upload_edges():
    from src.ai.attachments import classify_upload
    assert classify_upload("x.PNG", "application/octet-stream") == ("image", "image/png")
    assert classify_upload("photo", "image/jpeg") == ("image", "image/jpeg")
    assert classify_upload("doc.pdf", "") == ("pdf", "application/pdf")
    assert classify_upload("a.ts", "video/mp2t") == ("text", "video/mp2t")  # extension wins
    assert classify_upload("data.csv", "text/csv; charset=utf-8") == ("text", "text/csv")
    assert classify_upload("tool.exe", "application/octet-stream") is None
    assert classify_upload("clip.mp4", "video/mp4") is None


def test_build_user_content_shapes():
    from src.ai.attachments import build_user_content, TEXT_INLINE_LIMIT_CHARS
    assert build_user_content("plain", []) == "plain"
    img = SimpleNamespace(filename="s.png", kind="image", mime="image/png", size=3, data=b"abc")
    out = build_user_content("hi", [img])
    assert out[0] == {"type": "text", "text": "hi"}
    assert out[1]["image_url"]["url"] == "data:image/png;base64,YWJj"
    # Outside the replay window (no bytes): a loud note, plain string content.
    stale = SimpleNamespace(filename="s.png", kind="image", mime="image/png", size=2048, data=None)
    out = build_user_content("hi", [stale])
    assert isinstance(out, str) and "not re-sent: s.png (image, 2 KB)" in out
    # Huge text: the cut is announced, never silent.
    big = SimpleNamespace(filename="big.log", kind="text", mime="text/plain",
                          size=1, data=("x" * (TEXT_INLINE_LIMIT_CHARS + 10)).encode())
    out = build_user_content("", [big])
    assert "were not included in the prompt" in out and "```log" in out
