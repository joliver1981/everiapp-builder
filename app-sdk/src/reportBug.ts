/**
 * Programmatic API to submit a bug report from inside an AIHub-generated app.
 *
 * Usage:
 *   import { reportBug } from '@aihub/app-sdk'
 *   try { ... } catch (e) {
 *     await reportBug({ title: 'Save failed', description: String(e) })
 *   }
 */

import { captureScreenshot, installBugCapture, snapshotCapturedContext } from './bugCapture'

const AIHUB_BASE: string =
  ((import.meta as any).env?.VITE_AIHUB_BASE_URL as string | undefined) || ''

declare global {
  interface Window {
    __AIHUB_APP_ID__?: string
    __AIHUB_VERSION__?: number
    __AIHUB_DEPLOYMENT_ID__?: string
  }
}

// Start capturing the moment this module is loaded. Idempotent.
installBugCapture()

export interface ReportBugOptions {
  title: string
  description?: string
  reporterLabel?: string
  includeScreenshot?: boolean  // default: true
  extra?: Record<string, unknown>
}

export interface ReportBugResult {
  ok: boolean
  reportId?: string
  error?: string
}

function getAppId(): string | null {
  if (typeof window === 'undefined') return null
  if (window.__AIHUB_APP_ID__) return window.__AIHUB_APP_ID__
  const meta = document.querySelector('meta[name="aihub-app-id"]')
  return meta ? meta.getAttribute('content') : null
}

export async function reportBug(opts: ReportBugOptions): Promise<ReportBugResult> {
  const appId = getAppId()
  if (!appId) {
    return { ok: false, error: 'AIHub app id not found in window or <meta>' }
  }

  const captured = snapshotCapturedContext()
  if (opts.extra) (captured as any).extra = opts.extra
  // Trace spine: the session's trace id + the recent client spans (clicks,
  // dataset calls, errors — with timings) so the analyzer sees WHAT HAPPENED,
  // not just the console tail.
  try {
    const { getTraceId, getRecentSpans } = await import('./tracing')
    ;(captured as any).trace_id = getTraceId()
    // detail carries raw request bodies (dataset params, app-DB values) —
    // real business data. The spans TABLE encrypts it behind the capture
    // level; a bug report would store it verbatim, so strip it here. The
    // kind/name/status/error/timing chronology is what the analyzer needs.
    ;(captured as any).recent_spans = getRecentSpans().map(({ detail: _detail, ...s }) => s)
  } catch {
    /* tracing unavailable — report still goes out */
  }

  let screenshot: string | null = null
  if (opts.includeScreenshot !== false) {
    screenshot = await captureScreenshot()
  }

  const body = {
    title: opts.title,
    description: opts.description ?? '',
    reporter_label: opts.reporterLabel,
    version: typeof window !== 'undefined' ? window.__AIHUB_VERSION__ ?? null : null,
    deployment_id: typeof window !== 'undefined' ? window.__AIHUB_DEPLOYMENT_ID__ ?? null : null,
    captured_context: captured,
    screenshot_data_url: screenshot,
  }

  try {
    const resp = await fetch(`${AIHUB_BASE}/api/bug-reports/${appId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(body),
    })
    if (!resp.ok) {
      const text = await resp.text()
      return { ok: false, error: `HTTP ${resp.status}: ${text.slice(0, 200)}` }
    }
    const data = await resp.json().catch(() => ({}))
    return { ok: true, reportId: data?.id }
  } catch (e: any) {
    return { ok: false, error: e?.message || String(e) }
  }
}
