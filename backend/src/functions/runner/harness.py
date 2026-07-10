"""Child-process harness for app server functions. STDLIB ONLY.

Runs as:  python -B -s harness.py <path-to-function-file>

This file executes in the SERVER-FUNCTION interpreter (dev: the repo venv;
packaged: the vendored CPython embeddable) — never inside the platform
process, and it may not import anything beyond the standard library because
the embeddable distribution ships bare.

Protocol with the parent (backend/src/functions/service.py):
  stdin   one JSON document {"args": <any>, "meta": {app_id, base_url, token,
          user, fn_name, timeout_s}}
  stdout  ONE line "AIHUB_FN_RESULT:" + JSON envelope — {"ok": true,
          "result": ...} or {"ok": false, "error": {"message", "trace"}}.
          sys.stdout is swapped to stderr before user code loads, so a stray
          print() in a function can never corrupt the envelope; the parent
          returns captured stderr to the app as logs[].
  exit    always 0 — the outcome is in the envelope, and the parent treats a
          missing envelope (interpreter crash) as its own error class.

ctx.* capabilities are plain HTTP calls BACK to the platform's existing
app-facing routes, authenticated with the app-scoped token from meta. Every
gate, rate limit, size cap, and audit log those routes already enforce applies
unchanged; this file adds no capability of its own.
"""
from __future__ import annotations

import json
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path

SENTINEL = "AIHUB_FN_RESULT:"
MAX_RESULT_BYTES = 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# ctx — the platform bridge handed to handler(args, ctx)
# ---------------------------------------------------------------------------

class _PlatformBridge:
    def __init__(self, meta: dict):
        self._base = str(meta.get("base_url", "")).rstrip("/")
        self._token = meta.get("token") or ""
        # ctx calls are bounded per-call; the parent's hard kill at
        # timeout_s + 5 is the overall backstop.
        self._timeout = max(5, int(meta.get("timeout_s") or 30))

    def request(self, label: str, method: str, path: str, payload=None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self._base + path, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8")).get("detail", "")
            except Exception:
                pass
            # Platform error details are written to be fixable — propagate them
            # verbatim so they land in the function's error envelope.
            raise RuntimeError(f"{label}: HTTP {e.code}{' — ' + str(detail) if detail else ''}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"{label}: platform unreachable ({e.reason})")


class _Db:
    """The app's own SQLite store — same routes the browser SDK's useAppDB hits."""

    def __init__(self, bridge: _PlatformBridge, app_id: str):
        self._bridge = bridge
        self._app_id = app_id

    def query(self, sql: str, params: dict | None = None, scope: str | None = None,
              limit: int | None = None) -> dict:
        """Run a SELECT. Returns {rows, columns, row_count, truncated}.
        Pass limit= to raise the default 50k row cap (max 500k)."""
        payload: dict = {"sql": sql, "params": params or {}}
        if scope is not None:
            payload["scope"] = scope
        if limit is not None:
            payload["limit"] = int(limit)
        return self._bridge.request(
            "ctx.db.query", "POST", f"/api/apps/{self._app_id}/db/query", payload)

    def exec(self, sql: str, params: dict | None = None) -> dict:
        """Run an INSERT/UPDATE/DELETE. Returns {rows_affected, last_insert_rowid}."""
        return self._bridge.request(
            "ctx.db.exec", "POST", f"/api/apps/{self._app_id}/db/exec",
            {"sql": sql, "params": params or {}})


class Ctx:
    """Platform capabilities available to a server function.

    Same permission boundary as the app's browser code: connections must be
    admin-configured, app-callable, and attached to this app.
    """

    def __init__(self, meta: dict):
        self._bridge = _PlatformBridge(meta)
        self.app_id = str(meta.get("app_id", ""))
        self.user = meta.get("user") or {}
        self.db = _Db(self._bridge, self.app_id)
        self._conn_cache: list[dict] | None = None

    def log(self, *args) -> None:
        """Log a line — returned to the app in the invoke response's logs[]."""
        print(*args, file=sys.stderr)

    def call_connection(self, id_or_name: str, method: str = "GET", path: str = "/",
                        query: dict | None = None, headers: dict | None = None,
                        body=None) -> dict:
        """One outbound HTTP call through a bound, app-callable Connection.
        Returns {status, headers, body, truncated}."""
        return self._bridge.request(
            "ctx.call_connection", "POST",
            f"/api/apps/{self.app_id}/connections/{id_or_name}/call",
            {"method": method, "path": path, "query": query,
             "headers": headers, "body": body})

    def ai_decide(self, name: str, input: dict | None = None) -> dict:
        """Invoke one of the app's registered AI decisions.
        Returns {value, source, latency_ms} — never raises for LLM trouble."""
        return self._bridge.request(
            "ctx.ai_decide", "POST",
            f"/api/decisions/{self.app_id}/{name}/invoke", {"input": input or {}})

    # -- ai_chat ------------------------------------------------------------
    # KEEP IN SYNC with app-sdk/src/aiChat.ts — same provider-format mapping,
    # same resolve-don't-throw contract for upstream/gateway failures.

    def _connections(self) -> list[dict]:
        if self._conn_cache is None:
            self._conn_cache = self._bridge.request(
                "ctx.ai_chat", "GET", f"/api/apps/{self.app_id}/connections")
        return self._conn_cache

    def ai_chat(self, id_or_name: str, messages, model: str | None = None,
                system: str | None = None, max_tokens: int | None = None,
                temperature: float | None = None, extra: dict | None = None) -> dict:
        """One chat completion through an attached AI-provider Connection.
        Returns {status, text, model, raw, error} — check error / status >= 400."""
        conns = self._connections()
        conn = next((c for c in conns
                     if c.get("id") == id_or_name or c.get("name") == id_or_name), None)
        if not conn:
            raise RuntimeError(
                f"ctx.ai_chat: no attached connection with id or name '{id_or_name}' — "
                "attach it from this app's Data & APIs panel in the builder")
        if conn.get("kind") != "ai":
            raise RuntimeError(
                f"ctx.ai_chat: connection '{conn.get('name')}' is not an AI provider — "
                "use ctx.call_connection for generic HTTP calls")

        models = conn.get("models") or []
        use_model = model or conn.get("default_model") or (models[0] if models else None)
        if not use_model:
            raise RuntimeError(
                f"ctx.ai_chat: no model to use — pass model=, or set a default model "
                f"on connection '{conn.get('name')}' in Admin → Connections")

        msgs = [{"role": "user", "content": messages}] if isinstance(messages, str) else list(messages)
        system_parts = ([system] if system else []) + [
            m["content"] for m in msgs if m.get("role") == "system"]
        chat = [m for m in msgs if m.get("role") != "system"]

        api_format = conn.get("api_format")
        if api_format == "anthropic":
            body: dict = {
                "model": use_model,
                # Anthropic requires max_tokens; 4096 is valid for every model
                # generation (older models reject higher caps).
                "max_tokens": max_tokens if max_tokens is not None else 4096,
                "messages": chat,
            }
            if system_parts:
                body["system"] = "\n\n".join(system_parts)
            if temperature is not None:
                body["temperature"] = temperature
        else:
            # OpenAI-compatible. OpenAI/Azure deprecated max_tokens in favor of
            # max_completion_tokens; gateways like OpenRouter still take max_tokens.
            tokens_key = ("max_completion_tokens"
                          if conn.get("provider") in ("openai", "azure_openai")
                          else "max_tokens")
            body = {
                "model": use_model,
                "messages": [{"role": "system", "content": c} for c in system_parts] + chat,
            }
            if max_tokens is not None:
                body[tokens_key] = max_tokens
            if temperature is not None:
                body["temperature"] = temperature
        if extra:
            body.update(extra)

        path = conn.get("chat_path") or (
            "/messages" if api_format == "anthropic" else "/chat/completions")
        try:
            res = self.call_connection(conn["id"], method="POST", path=path, body=body)
        except RuntimeError as e:
            # Stale metadata heals on the next attempt; upstream gateway
            # failures resolve instead of raising (long generations can exceed
            # the connection's timeout) — platform-side failures still raise.
            self._conn_cache = None
            msg = str(e)
            for code in ("502", "504"):
                if f"HTTP {code}" in msg:
                    return {"status": int(code), "text": "", "model": use_model,
                            "raw": None, "error": msg}
            raise

        status = res.get("status", 0)
        raw = res.get("body")
        return {
            "status": status,
            "text": _extract_text(api_format, raw) if status < 400 else "",
            "model": use_model,
            "raw": raw,
            "error": _extract_error(status, raw),
        }


def _extract_text(api_format, body) -> str:
    if api_format == "anthropic":
        blocks = body.get("content") if isinstance(body, dict) else None
        if isinstance(blocks, list):
            return "".join(str(b.get("text", "")) for b in blocks
                           if isinstance(b, dict) and b.get("type") == "text")
        return ""
    try:
        content = body["choices"][0]["message"]["content"]
        return content if isinstance(content, str) else ""
    except (KeyError, IndexError, TypeError):
        return ""


def _extract_error(status, body):
    if status < 400:
        return None
    if isinstance(body, dict):
        err = body.get("error")
        msg = err.get("message") if isinstance(err, dict) else err
        msg = msg or body.get("message")
        if isinstance(msg, str) and msg:
            return msg
    if isinstance(body, str) and body:
        return body[:300]
    try:
        return json.dumps(body)[:300]
    except Exception:
        return f"HTTP {status}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _emit(real_stdout, envelope: dict) -> None:
    real_stdout.write(SENTINEL + json.dumps(envelope) + "\n")
    real_stdout.flush()


def _error_envelope(message: str, exc: BaseException | None = None) -> dict:
    trace = ""
    if exc is not None:
        frames = traceback.format_exception(type(exc), exc, exc.__traceback__)
        trace = "".join(frames[-3:]).strip()
    return {"ok": False, "error": {"message": message, "trace": trace}}


def main() -> None:
    real_stdout = sys.stdout
    # Reserve the real stdout for the envelope: user print() goes to stderr,
    # which the parent returns as logs[].
    sys.stdout = sys.stderr

    try:
        req = json.loads(sys.stdin.read())
        args = req.get("args")
        meta = req.get("meta") or {}
        fn_path = Path(sys.argv[1]).resolve()
    except Exception as e:
        _emit(real_stdout, _error_envelope(f"harness could not read its input: {e}", e))
        return

    # server/ on sys.path: enables `from sdk import Ctx` type hints and sibling
    # helper modules next to the function file.
    server_dir = fn_path.parent.parent
    for p in (str(server_dir), str(fn_path.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)
    # Admin-installed packages (Admin → Python Packages): after the app's own
    # server/ dirs (app code always wins) but before the interpreter's paths —
    # so an admin-installed newer copy of a bundled library shadows it.
    for p in meta.get("extra_sys_path") or []:
        if p not in sys.path:
            sys.path.insert(2, p)

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(f"_aihub_fn_{fn_path.stem}", fn_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except BaseException as e:
        _emit(real_stdout, _error_envelope(
            f"{type(e).__name__} while loading the function: {e}. Server functions may "
            "import the standard library, the platform's curated libraries, packages "
            "an admin has installed (Admin → Python Packages), and files under "
            "server/ — nothing else. If this import is a real library, ask an admin "
            "to install it under Admin → Python Packages.", e))
        return

    handler = getattr(module, "handler", None)
    if not callable(handler):
        _emit(real_stdout, _error_envelope(
            "the function file must define `def handler(args, ctx):`"))
        return

    try:
        result = handler(args, Ctx(meta))
    except BaseException as e:
        _emit(real_stdout, _error_envelope(f"{type(e).__name__}: {e}", e))
        return

    try:
        payload = json.dumps(result)
    except (TypeError, ValueError) as e:
        _emit(real_stdout, _error_envelope(
            "the function's return value is not JSON-serializable — convert "
            "DataFrames with .to_dict('records'), numpy values with .item()/list(), "
            f"dates with .isoformat() ({e})", e))
        return
    if len(payload) > MAX_RESULT_BYTES:
        _emit(real_stdout, _error_envelope(
            f"the function's result is {len(payload)} bytes (cap 5 MiB) — "
            "aggregate server-side and return less"))
        return

    _emit(real_stdout, {"ok": True, "result": result})


if __name__ == "__main__":
    main()
