/**
 * <BugReportButton/>
 *
 * Floating "Report a bug" button you mount once at the root of the app.
 * Auto-decides whether to render based on the app's widget config (fetched
 * from AIHub on mount). Set `force` to override.
 *
 * The visual design is intentionally minimal/dependency-free so it doesn't
 * collide with the host app's CSS. All styles are inline.
 */

import type { CSSProperties, ReactNode } from 'react'
import { useEffect, useState } from 'react'

import { reportBug } from './reportBug'

const AIHUB_BASE: string =
  ((import.meta as any).env?.VITE_AIHUB_BASE_URL as string | undefined) || ''

interface WidgetConfig {
  bug_widget_enabled: boolean
}

interface Props {
  /** Position the floating button. Default: 'bottom-right'. */
  position?: 'bottom-right' | 'bottom-left' | 'top-right' | 'top-left'
  /** Skip the widget-config fetch and render unconditionally. */
  force?: boolean
}

export function BugReportButton({ position = 'bottom-right', force = false }: Props) {
  const [enabled, setEnabled] = useState<boolean>(force)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (force) {
      setEnabled(true)
      return
    }
    const appId =
      (typeof window !== 'undefined' && (window as any).__AIHUB_APP_ID__) ||
      document.querySelector('meta[name="aihub-app-id"]')?.getAttribute('content')
    if (!appId) {
      setEnabled(false)
      return
    }
    fetch(`${AIHUB_BASE}/api/apps/${appId}/widget-config`)
      .then((r) => (r.ok ? (r.json() as Promise<WidgetConfig>) : null))
      .then((cfg) => setEnabled(!!cfg?.bug_widget_enabled))
      .catch(() => setEnabled(false))
  }, [force])

  if (!enabled) return null

  const corner = positionStyles(position)

  return (
    <>
      <button
        type="button"
        aria-label="Report a bug"
        onClick={() => setOpen(true)}
        style={{
          position: 'fixed',
          ...corner,
          zIndex: 2147483646,
          width: 44,
          height: 44,
          borderRadius: 22,
          background: '#ef4444',
          color: 'white',
          border: 'none',
          boxShadow: '0 4px 12px rgba(0,0,0,0.18)',
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 18,
        }}
        title="Report a bug"
      >
        <BugIcon />
      </button>
      {open && <BugReportModal onClose={() => setOpen(false)} />}
    </>
  )
}

function BugIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="8" y="6" width="8" height="14" rx="4" />
      <path d="M19 7l-3 2M5 7l3 2M19 13h-3M5 13h3M19 19l-3-2M5 19l3-2M9 6V4a3 3 0 0 1 6 0v2" />
    </svg>
  )
}

function BugReportModal({ onClose }: { onClose: () => void }) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [reporter, setReporter] = useState('')
  const [includeScreenshot, setIncludeScreenshot] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null)

  const submit = async () => {
    if (!title.trim()) return
    setSubmitting(true)
    setResult(null)
    try {
      const r = await reportBug({
        title: title.trim(),
        description: description.trim(),
        reporterLabel: reporter.trim() || undefined,
        includeScreenshot,
      })
      if (r.ok) {
        setResult({ ok: true, message: 'Thanks — your report was sent.' })
        setTitle('')
        setDescription('')
        // auto-close after a beat so the user sees the confirmation
        setTimeout(onClose, 1500)
      } else {
        setResult({ ok: false, message: r.error || 'Failed to submit.' })
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.55)',
        zIndex: 2147483647,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'white',
          color: '#111',
          borderRadius: 12,
          padding: 20,
          width: '92%',
          maxWidth: 460,
          boxShadow: '0 12px 32px rgba(0,0,0,0.25)',
          fontFamily: 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
        }}
      >
        <h2 style={{ margin: '0 0 4px 0', fontSize: 18, fontWeight: 600 }}>Report a bug</h2>
        <p style={{ margin: '0 0 16px 0', fontSize: 12, color: '#666' }}>
          We'll capture this page's URL, recent console output, and any failed network requests.
        </p>

        <Field label="Title">
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="What went wrong?"
            disabled={submitting}
            style={inputStyle}
          />
        </Field>

        <Field label="Details (optional)">
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Steps to reproduce, what you expected, etc."
            rows={4}
            disabled={submitting}
            style={{ ...inputStyle, resize: 'vertical' }}
          />
        </Field>

        <Field label="Your name or email (optional)">
          <input
            value={reporter}
            onChange={(e) => setReporter(e.target.value)}
            placeholder="So we can follow up"
            disabled={submitting}
            style={inputStyle}
          />
        </Field>

        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, marginTop: 4 }}>
          <input
            type="checkbox"
            checked={includeScreenshot}
            onChange={(e) => setIncludeScreenshot(e.target.checked)}
            disabled={submitting}
          />
          Attach a screenshot of this page
        </label>

        {result && (
          <div
            style={{
              marginTop: 12,
              padding: '8px 10px',
              borderRadius: 8,
              fontSize: 13,
              background: result.ok ? '#dcfce7' : '#fee2e2',
              color: result.ok ? '#15803d' : '#b91c1c',
            }}
          >
            {result.message}
          </div>
        )}

        <div style={{ marginTop: 16, display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button onClick={onClose} disabled={submitting} style={btnSecondaryStyle}>
            Cancel
          </button>
          <button onClick={submit} disabled={submitting || !title.trim()} style={btnPrimaryStyle}>
            {submitting ? 'Sending…' : 'Send report'}
          </button>
        </div>
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <label style={{ display: 'block', fontSize: 12, fontWeight: 500, color: '#374151', marginBottom: 4 }}>
        {label}
      </label>
      {children}
    </div>
  )
}

const inputStyle: CSSProperties = {
  width: '100%',
  boxSizing: 'border-box',
  padding: '8px 10px',
  borderRadius: 8,
  border: '1px solid #d1d5db',
  fontSize: 13,
  fontFamily: 'inherit',
  outline: 'none',
}

const btnPrimaryStyle: CSSProperties = {
  padding: '8px 14px',
  borderRadius: 8,
  border: 'none',
  background: '#ef4444',
  color: 'white',
  fontSize: 13,
  fontWeight: 500,
  cursor: 'pointer',
}

const btnSecondaryStyle: CSSProperties = {
  padding: '8px 14px',
  borderRadius: 8,
  border: '1px solid #d1d5db',
  background: 'white',
  color: '#374151',
  fontSize: 13,
  cursor: 'pointer',
}

function positionStyles(p: NonNullable<Props['position']>): CSSProperties {
  switch (p) {
    case 'bottom-left':
      return { bottom: 16, left: 16 }
    case 'top-right':
      return { top: 16, right: 16 }
    case 'top-left':
      return { top: 16, left: 16 }
    case 'bottom-right':
    default:
      return { bottom: 16, right: 16 }
  }
}
