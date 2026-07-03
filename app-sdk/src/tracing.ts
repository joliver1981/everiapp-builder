/**
 * AIHub App SDK — client side of the trace spine.
 *
 * One trace id per app session (page load), sent as `X-AIHub-Trace-Id` on
 * every request the app makes to the AIHub platform. The backend's trace
 * middleware picks it up, so dataset queries, app-DB calls, AI calls — and
 * the LLM spans + cost rows they produce — all join to this session.
 *
 * Installed by patching window.fetch (same pattern as bugCapture): one choke
 * point covers the SDK's own calls AND any fetch the generated app makes to
 * the platform directly. Non-platform URLs are never touched, and a tracing
 * failure must never break the request itself.
 */

declare global {
  interface Window {
    __AIHUB_TRACE_ID__?: string
  }
}

const AIHUB_BASE: string =
  ((import.meta as any).env?.VITE_AIHUB_BASE_URL as string | undefined) || ''

const TRACE_HEADER = 'X-AIHub-Trace-Id'

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

let installed = false

export function installTracing(): void {
  if (installed) return
  installed = true
  if (typeof window === 'undefined' || typeof window.fetch !== 'function') return

  const originalFetch = window.fetch.bind(window)
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    try {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : (input as Request).url || ''
      if (isPlatformUrl(url)) {
        const headers = new Headers(
          init?.headers ?? (input instanceof Request ? input.headers : undefined),
        )
        if (!headers.has(TRACE_HEADER)) headers.set(TRACE_HEADER, getTraceId())
        init = { ...(init || {}), headers }
      }
    } catch {
      // Never break a request over tracing.
    }
    return originalFetch(input as any, init)
  }
}

installTracing()
