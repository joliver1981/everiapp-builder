/**
 * AIHub App SDK — client side of the trace spine.
 *
 * One trace id per app session (page load), sent as `X-AIHub-Trace-Id` on
 * every request the app makes to the AIHub platform, so dataset queries,
 * app-DB calls, AI calls — and the LLM spans + cost rows they produce — all
 * join to this session.
 *
 * This module also EMITS client-side spans: every platform call is timed and
 * classified (dataset.query / appdb.call / http.call), uncaught errors and
 * unhandled rejections become ui.error spans, and clicks on interactive
 * elements become ui.interaction spans (the "You clicked Save" line in story
 * mode). Spans buffer in memory and flush in small batches to
 * POST /api/apps/{app_id}/spans; a ring buffer of recent spans is kept for
 * bug reports regardless of flushing.
 *
 * Installed by patching window.fetch (same pattern as bugCapture): one choke
 * point covers the SDK's own calls AND any fetch the generated app makes to
 * the platform directly. Non-platform URLs are never touched, and a tracing
 * failure must never break the request itself.
 */

declare global {
  interface Window {
    __AIHUB_TRACE_ID__?: string
    __AIHUB_APP_ID__?: string
    __AIHUB_TOKEN__?: string
  }
}

const AIHUB_BASE: string =
  ((import.meta as any).env?.VITE_AIHUB_BASE_URL as string | undefined) || ''

const TRACE_HEADER = 'X-AIHub-Trace-Id'
const FLUSH_INTERVAL_MS = 500
const FLUSH_BATCH_MAX = 25
const BUFFER_MAX = 200
const RING_MAX = 100
const DETAIL_MAX = 2000

export interface ClientSpan {
  kind: 'dataset.query' | 'appdb.call' | 'http.call' | 'ui.error' | 'ui.interaction'
  name: string
  trace_id: string
  status: 'ok' | 'error'
  error?: string
  latency_ms: number
  detail?: string
  /** Client timestamp (ms epoch) — kept in the ring buffer for bug reports. */
  ts: number
}

function makeId(): string {
  try {
    return crypto.randomUUID()
  } catch {
    // Very old browsers / non-secure contexts: good-enough fallback.
    return `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
  }
}

/** The session's trace id (created on first use, stable until page unload). */
export function getTraceId(): string {
  if (typeof window === 'undefined') return ''
  if (!window.__AIHUB_TRACE_ID__) {
    window.__AIHUB_TRACE_ID__ = makeId()
  }
  return window.__AIHUB_TRACE_ID__
}

function isPlatformUrl(url: string): boolean {
  // Same-origin platform calls ('/api/...'), or absolute calls to the
  // configured platform base (deployed/embedded apps).
  if (url.startsWith('/api/')) return true
  if (AIHUB_BASE && url.startsWith(`${AIHUB_BASE}/api/`)) return true
  return false
}

// ------------------------------------------------------------------ spans

const buffer: Omit<ClientSpan, 'ts'>[] = []
const ring: ClientSpan[] = []
let flushTimer: ReturnType<typeof setInterval> | null = null
let originalFetch: typeof fetch | null = null
let warnedAuthOnce = false

/** Recent spans (newest last) — attached to bug reports. */
export function getRecentSpans(): ClientSpan[] {
  return ring.slice()
}

export function emitSpan(span: Omit<ClientSpan, 'ts' | 'trace_id'>): void {
  try {
    const full: ClientSpan = { ...span, trace_id: getTraceId(), ts: Date.now() }
    ring.push(full)
    while (ring.length > RING_MAX) ring.shift()
    if (buffer.length < BUFFER_MAX) {
      const { ts: _ts, ...wire } = full
      buffer.push(wire)
    }
    if (!flushTimer && typeof setInterval === 'function') {
      flushTimer = setInterval(flushSpans, FLUSH_INTERVAL_MS)
    }
  } catch {
    /* tracing must never break the app */
  }
}

function ingestionUrl(): string | null {
  const appId = window.__AIHUB_APP_ID__
  if (!appId) return null
  return `${AIHUB_BASE}/api/apps/${appId}/spans`
}

function flushSpans(): void {
  try {
    const url = ingestionUrl()
    if (!url || buffer.length === 0 || !originalFetch) return
    const spans = buffer.splice(0, FLUSH_BATCH_MAX)
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      [TRACE_HEADER]: getTraceId(),
    }
    if (window.__AIHUB_TOKEN__) headers['Authorization'] = `Bearer ${window.__AIHUB_TOKEN__}`
    // originalFetch: the flush request itself must never be traced (recursion).
    originalFetch(url, {
      method: 'POST',
      headers,
      credentials: 'include',
      body: JSON.stringify({ spans }),
    }).then((resp) => {
      if ((resp.status === 401 || resp.status === 403) && !warnedAuthOnce) {
        warnedAuthOnce = true
        // A silently frozen trace is undebuggable — say why, once.
        console.warn(
          `[AIHub SDK] Trace upload rejected (HTTP ${resp.status}) — the preview's auth token ` +
          'likely expired. Reload the preview to resume tracing, or enable the bug widget ' +
          'to allow tokenless telemetry for this app.',
        )
      }
    }).catch(() => {
      /* best-effort; dropped on failure */
    })
  } catch {
    /* ignore */
  }
}

// ---------------------------------------------------- URL classification

function classify(url: string): { kind: ClientSpan['kind']; name: string; errorsOnly?: boolean } | null {
  const path = url.startsWith('/') ? url : url.slice(AIHUB_BASE.length)
  // Never client-traced: the ingestion endpoint itself and bug-report posts.
  if (/\/api\/apps\/[^/]+\/spans$/.test(path) || path.startsWith('/api/bug-reports/')) return null
  // Decision invokes: the server emits a richer ai.decision span on success,
  // but a 404/401/network failure never reaches the server's tracer — record
  // ONLY failures client-side so those aren't a blind spot.
  const dm = path.match(/^\/api\/decisions\/[^/]+\/([^/]+)\/invoke$/)
  if (dm) return { kind: 'http.call', name: `decision:${dm[1]}`, errorsOnly: true }
  let m = path.match(/^\/api\/apps\/[^/]+\/datasets\/([^/]+)\/(execute|mutate)$/)
  if (m) return { kind: 'dataset.query', name: m[1] }
  m = path.match(/^\/api\/apps\/[^/]+\/db\/([^?]+)/)
  if (m) return { kind: 'appdb.call', name: m[1] }
  m = path.match(/^\/api\/([^?]*)/)
  if (m) return { kind: 'http.call', name: m[1].slice(0, 100) }
  return null
}

function bodySnippet(init?: RequestInit): string | undefined {
  try {
    if (init && typeof init.body === 'string' && init.body.length <= DETAIL_MAX) {
      return init.body
    }
  } catch {
    /* ignore */
  }
  return undefined
}

// ------------------------------------------------------------- listeners

function labelFor(el: Element | null): string | null {
  while (el && el !== document.body) {
    const tag = el.tagName
    if (tag === 'BUTTON' || tag === 'A' || (el as HTMLElement).getAttribute?.('role') === 'button') {
      const text = ((el as HTMLElement).innerText || (el as HTMLElement).getAttribute('aria-label') || '')
        .trim().replace(/\s+/g, ' ').slice(0, 60)
      return text || tag.toLowerCase()
    }
    el = el.parentElement
  }
  return null
}

function installListeners(): void {
  window.addEventListener('error', (ev) => {
    emitSpan({
      kind: 'ui.error', name: 'window.error', status: 'error',
      error: String(ev.message || ev.error || 'unknown error').slice(0, 2000),
      latency_ms: 0,
      detail: ev.filename ? JSON.stringify({ file: ev.filename, line: ev.lineno }) : undefined,
    })
  })
  window.addEventListener('unhandledrejection', (ev) => {
    emitSpan({
      kind: 'ui.error', name: 'unhandledrejection', status: 'error',
      error: String((ev as PromiseRejectionEvent).reason ?? 'unhandled rejection').slice(0, 2000),
      latency_ms: 0,
    })
  })
  document.addEventListener('click', (ev) => {
    const label = labelFor(ev.target as Element | null)
    if (label) {
      emitSpan({ kind: 'ui.interaction', name: label, status: 'ok', latency_ms: 0 })
    }
  }, { capture: true, passive: true })
}

// ---------------------------------------------------------------- install

let installed = false

export function installTracing(): void {
  if (installed) return
  installed = true
  if (typeof window === 'undefined' || typeof window.fetch !== 'function') return

  originalFetch = window.fetch.bind(window)
  const base = originalFetch
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    let url = ''
    let platform = false
    try {
      url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : (input as Request).url || ''
      platform = isPlatformUrl(url)
      if (platform) {
        const headers = new Headers(
          init?.headers ?? (input instanceof Request ? input.headers : undefined),
        )
        if (!headers.has(TRACE_HEADER)) headers.set(TRACE_HEADER, getTraceId())
        init = { ...(init || {}), headers }
      }
    } catch {
      // Never break a request over tracing.
    }

    const cls = platform ? classify(url) : null
    if (!cls) return base(input as any, init)

    const t0 = Date.now()
    const detail = bodySnippet(init)
    return base(input as any, init).then(
      (resp) => {
        if (!(cls.errorsOnly && resp.ok)) {
          emitSpan({
            kind: cls.kind, name: cls.name,
            status: resp.ok ? 'ok' : 'error',
            error: resp.ok ? undefined : `HTTP ${resp.status}`,
            latency_ms: Date.now() - t0, detail,
          })
        }
        return resp
      },
      (err) => {
        emitSpan({
          kind: cls.kind, name: cls.name, status: 'error',
          error: String(err).slice(0, 2000),
          latency_ms: Date.now() - t0, detail,
        })
        throw err
      },
    )
  }

  installListeners()
}

installTracing()
