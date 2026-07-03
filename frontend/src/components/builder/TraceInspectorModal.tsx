/**
 * TraceInspectorModal — the app's runtime trace, two lenses:
 *   Story: plain-English chronology ("You clicked Save → dataset refused...")
 *   Timeline: per-span rows with kind/status/latency/cost.
 * Sessions are grouped by trace_id (one id per app session, minted by the SDK).
 * Metadata only — payload viewing arrives with the audited decrypt path.
 */
import { useCallback, useEffect, useState } from 'react'
import { Activity, AlertCircle, Loader2, RefreshCw, X } from 'lucide-react'
import { apiClient } from '@/api/client'
import { cn } from '@/lib/utils'

interface SpanRow {
  id: string
  trace_id: string | null
  kind: string
  purpose: string
  name: string | null
  provider_type: string
  model: string
  status: string
  error: string | null
  has_prompt: boolean
  has_response: boolean
  input_tokens: number
  output_tokens: number
  cost_usd: number
  latency_ms: number
  created_at: string | null
}

function narrate(s: SpanRow): string {
  const name = s.name || s.purpose || s.kind
  const ms = s.latency_ms ? ` — ${s.latency_ms}ms` : ''
  const failed = s.status === 'error'
  switch (s.kind) {
    case 'ui.interaction':
      return `You clicked “${name}”`
    case 'ui.error':
      return `Something broke on screen: ${s.error || 'unknown error'}`
    case 'dataset.query':
      return failed
        ? `Dataset “${name}” refused: ${s.error || 'unknown error'}`
        : `The app asked dataset “${name}”${ms}`
    case 'appdb.call':
      return failed
        ? `The app's local database call (${name}) failed: ${s.error || ''}`
        : `The app used its local database (${name})${ms}`
    case 'ai.call':
      return failed
        ? `The AI call (${s.purpose || name}) failed: ${s.error || ''}`
        : `The app asked AI (${s.purpose || name})${ms}${s.cost_usd ? ` · $${s.cost_usd.toFixed(4)}` : ''}`
    default:
      return failed
        ? `Platform call ${name} failed: ${s.error || ''}`
        : `The app called the platform (${name})${ms}`
  }
}

const timeOf = (s: SpanRow) =>
  s.created_at ? new Date(s.created_at + (s.created_at.endsWith('Z') ? '' : 'Z')) : new Date(0)

export function TraceInspectorModal({ appId, onClose }: { appId: string; onClose: () => void }) {
  const [spans, setSpans] = useState<SpanRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [mode, setMode] = useState<'story' | 'timeline'>('story')
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setSpans(await apiClient.get<SpanRow[]>(`/apps/${appId}/spans?limit=500`))
      setError(null)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [appId])

  useEffect(() => { load() }, [load])

  // Group into sessions by trace_id, newest session first; chronological within.
  const sessions: Array<{ traceId: string | null; rows: SpanRow[] }> = []
  if (spans) {
    const byTrace = new Map<string, SpanRow[]>()
    for (const s of spans) {
      const key = s.trace_id || '(untraced)'
      if (!byTrace.has(key)) byTrace.set(key, [])
      byTrace.get(key)!.push(s)
    }
    for (const [key, rows] of byTrace) {
      rows.sort((a, b) => timeOf(a).getTime() - timeOf(b).getTime())
      sessions.push({ traceId: key === '(untraced)' ? null : key, rows })
    }
    sessions.sort((a, b) =>
      timeOf(b.rows[b.rows.length - 1]).getTime() - timeOf(a.rows[a.rows.length - 1]).getTime())
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="flex max-h-[85vh] w-[720px] flex-col rounded-xl border border-border bg-card shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <div className="flex items-center gap-2">
            <Activity size={16} className="text-primary" />
            <h3 className="text-sm font-semibold">App trace</h3>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex rounded-lg bg-secondary p-0.5">
              {(['story', 'timeline'] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={cn(
                    'rounded-md px-3 py-1 text-xs capitalize',
                    mode === m ? 'bg-card font-medium shadow-sm' : 'text-muted-foreground',
                  )}
                >
                  {m}
                </button>
              ))}
            </div>
            <button onClick={load} title="Refresh"
                    className="rounded p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground">
              {loading ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
            </button>
            <button onClick={onClose} className="rounded p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground">
              <X size={16} />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {error && (
            <p className="text-sm text-destructive">Couldn't load spans: {error}</p>
          )}
          {spans && sessions.length === 0 && !error && (
            <div className="py-10 text-center text-sm text-muted-foreground">
              <Activity size={28} className="mx-auto mb-2 opacity-30" />
              No trace yet — open the app preview and click around; every
              dataset call, AI call, and error shows up here.
            </div>
          )}
          {sessions.map(({ traceId, rows }) => (
            <div key={traceId ?? 'untraced'} className="mb-5">
              <p className="mb-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                Session {traceId ? traceId.slice(0, 8) : '(untraced)'} · {rows.length} events
              </p>
              <div className="rounded-lg border border-border">
                {rows.map((s) => (
                  <div
                    key={s.id}
                    className={cn(
                      'flex items-start gap-3 border-b border-border px-3 py-2 text-xs last:border-b-0',
                      s.status === 'error' && 'bg-destructive/5',
                    )}
                  >
                    <span className="w-14 shrink-0 pt-0.5 font-mono text-[10px] text-muted-foreground">
                      {s.created_at ? timeOf(s).toLocaleTimeString() : ''}
                    </span>
                    {s.status === 'error' && (
                      <AlertCircle size={13} className="mt-0.5 shrink-0 text-destructive" />
                    )}
                    {mode === 'story' ? (
                      <span className={cn('leading-5', s.status === 'error' && 'text-destructive')}>
                        {narrate(s)}
                      </span>
                    ) : (
                      <span className="flex flex-1 items-center gap-2 leading-5">
                        <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">{s.kind}</span>
                        <span className="font-medium">{s.name || s.purpose}</span>
                        {s.model && <span className="text-muted-foreground">{s.provider_type}/{s.model}</span>}
                        <span className="ml-auto flex items-center gap-2 text-muted-foreground">
                          {s.input_tokens + s.output_tokens > 0 && (
                            <span>{s.input_tokens}→{s.output_tokens} tok</span>
                          )}
                          {s.cost_usd > 0 && <span>${s.cost_usd.toFixed(4)}</span>}
                          <span>{s.latency_ms}ms</span>
                        </span>
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
