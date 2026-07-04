/**
 * TraceInspectorModal — the app's runtime trace, two lenses:
 *   Story: plain-English chronology ("You clicked Save → dataset refused...")
 *   Timeline: per-span rows with kind/status/latency/cost.
 * Sessions are grouped by trace_id (one id per app session, minted by the SDK).
 * Metadata only — payload viewing arrives with the audited decrypt path.
 */
import { useCallback, useEffect, useState } from 'react'
import { Activity, AlertCircle, Loader2, RefreshCw, Sparkles, Trash2, Wrench, X } from 'lucide-react'
import { apiClient } from '@/api/client'
import { useChatStore } from '@/stores/chatStore'
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

interface Issue {
  key: string
  label: string
  count: number
  severity: 'error' | 'warn'
}

/** Rules-only issue detection (the copilot's Observe level — zero LLM cost). */
function detectIssues(spans: SpanRow[]): Issue[] {
  const byKey = new Map<string, Issue>()
  const bump = (key: string, label: string, severity: Issue['severity']) => {
    const cur = byKey.get(key)
    if (cur) cur.count += 1
    else byKey.set(key, { key, label, count: 1, severity })
  }
  for (const s of spans) {
    // The platform's own AI (diagnosis, bug analysis) is traced too — don't
    // report it as an app issue (a 60s diagnosis isn't a slow app).
    if (s.purpose === 'copilot_diagnose' || s.purpose === 'bug_analysis') continue
    if (s.kind === 'ai.decision' && s.status === 'error') {
      bump(`fb:${s.name}`, `Decision “${s.name}” falling back`, 'warn')
    } else if (s.status === 'error') {
      const sig = (s.error || '').slice(0, 60)
      bump(`err:${s.kind}:${s.name}:${sig}`,
           `${s.kind === 'ui.error' ? 'On-screen error' : `${s.name || s.kind} failing`}: ${sig || 'unknown'}`,
           'error')
    } else if (s.latency_ms > 2000 && s.kind !== 'ui.interaction') {
      bump(`slow:${s.kind}:${s.name}`, `${s.name || s.kind} is slow (>${Math.round(s.latency_ms / 1000)}s)`, 'warn')
    }
  }
  return [...byKey.values()].sort((a, b) =>
    (a.severity === b.severity ? b.count - a.count : a.severity === 'error' ? -1 : 1))
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
    case 'ai.decision':
      return failed
        ? `The AI decision “${name}” used its fallback: ${s.error || 'unknown reason'}`
        : `The app asked AI to decide “${name}”${ms}`
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

export function TraceInspectorModal({ appId, onClose, variant = 'modal', onFixRequested }: {
  appId: string
  onClose: () => void
  /** 'panel' renders as a full-height side panel (beside the Preview) instead
      of a centered modal — same content, different chrome. */
  variant?: 'modal' | 'panel'
  /** Called after "Fix it" hands the fix brief to the builder chat — the host
      page can switch the left panel to chat so the user watches it run. */
  onFixRequested?: () => void
}) {
  const [spans, setSpans] = useState<SpanRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [mode, setMode] = useState<'story' | 'timeline'>('story')
  const [loading, setLoading] = useState(false)
  const [live, setLive] = useState(true)

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      setSpans(await apiClient.get<SpanRow[]>(`/apps/${appId}/spans?limit=500`))
      setError(null)
    } catch (e: any) {
      setError(e.message)
    } finally {
      if (!silent) setLoading(false)
    }
  }, [appId])

  useEffect(() => { load() }, [load])

  // Live follow: poll quietly while the modal is open (spans land async via
  // the batched writer, so a running preview streams in near-real-time).
  useEffect(() => {
    if (!live) return
    const t = setInterval(() => load(true), 3000)
    return () => clearInterval(t)
  }, [live, load])

  const issues = spans ? detectIssues(spans) : []

  // "Explain" (copilot Suggest level): one diagnosis at a time.
  const [diagnosing, setDiagnosing] = useState<string | null>(null)
  const [diagnosis, setDiagnosis] = useState<{
    issueKey: string
    issueLabel: string
    diagnosis: string
    root_cause: string
    risk_level: string
    files_implicated: Array<{ path: string; action: string }>
  } | null>(null)
  const [diagError, setDiagError] = useState<string | null>(null)

  // "Fix it" (copilot Co-fix level): the diagnosis becomes a fix brief sent
  // through the normal builder chat — visible as a turn, so the user watches
  // the fix, self-heal, and verification run on the existing rails.
  const sendChatMessage = useChatStore((s) => s.sendMessage)
  const isStreaming = useChatStore((s) => s.isStreaming)

  const requestFix = () => {
    if (!diagnosis || isStreaming) return
    const episode = [...(spans || [])].slice(0, 30).reverse().map((s) =>
      `- [${s.kind}] ${s.name || s.purpose} — ${s.status}` +
      (s.error ? ` | ${s.error.slice(0, 200)}` : '') +
      (s.latency_ms ? ` | ${s.latency_ms}ms` : ''),
    ).join('\n')
    const brief = [
      `Fix this issue I hit while testing the app (found by the trace Inspector):`,
      ``,
      `ISSUE: ${diagnosis.issueLabel}`,
      `AI DIAGNOSIS: ${diagnosis.diagnosis}`,
      `ROOT CAUSE: ${diagnosis.root_cause}`,
      diagnosis.files_implicated.length
        ? `LIKELY FILES: ${diagnosis.files_implicated.map((f) => f.path).join(', ')}`
        : '',
      ``,
      `Traced events (oldest first):`,
      episode,
      ``,
      `Make the smallest correct fix. If the root cause is a decision missing from`,
      `the registry, re-emit a complete, valid decisions.json.`,
    ].filter((l) => l !== '').join('\n')
    sendChatMessage(appId, brief)
    setDiagnosis(null)
    onFixRequested?.()
  }

  const explain = async (issue: Issue) => {
    if (diagnosing) return
    setDiagnosing(issue.key)
    setDiagError(null)
    try {
      const windowSpans = (spans || []).slice(0, 50).map((s) => ({
        kind: s.kind, name: s.name || s.purpose, status: s.status,
        error: s.error || undefined, latency_ms: s.latency_ms, ts: s.created_at,
      }))
      const data = await apiClient.post<Omit<NonNullable<typeof diagnosis>, 'issueKey'>>(
        `/copilot/${appId}/diagnose`,
        { issue_label: issue.label, trace_id: spans?.[0]?.trace_id ?? null, spans: windowSpans },
      )
      setDiagnosis({ ...data, issueKey: issue.key, issueLabel: issue.label })
    } catch (e: any) {
      setDiagError(e.message)
    } finally {
      setDiagnosing(null)
    }
  }

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

  const content = (
      <>
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
            <button
              onClick={() => setLive((v) => !v)}
              title={live ? 'Live follow ON — polling every 3s' : 'Live follow OFF'}
              className={cn('rounded px-2 py-1 text-[10px] font-medium',
                live ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground')}
            >
              ● Live
            </button>
            <button onClick={() => load()} title="Refresh"
                    className="rounded p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground">
              {loading ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
            </button>
            <button
              onClick={async () => {
                if (!confirm('Clear this app\'s entire trace? This deletes all recorded events.')) return
                try {
                  await apiClient.delete(`/apps/${appId}/spans`)
                  setDiagnosis(null)
                  setSpans([])
                } catch (e: any) {
                  setError(e.message)
                }
              }}
              title="Clear the trace (deletes all recorded events for this app)"
              className="rounded p-1.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
            >
              <Trash2 size={14} />
            </button>
            <button onClick={onClose} className="rounded p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground">
              <X size={16} />
            </button>
          </div>
        </div>

        {issues.length > 0 && (
          <div className="border-b border-border px-4 py-2">
            <div className="flex flex-wrap gap-1.5">
              {issues.slice(0, 6).map((i) => (
                <button
                  key={i.key}
                  onClick={() => explain(i)}
                  disabled={!!diagnosing}
                  className={cn(
                    'flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] transition-colors disabled:opacity-60',
                    i.severity === 'error'
                      ? 'bg-destructive/10 text-destructive hover:bg-destructive/20'
                      : 'bg-warning/10 text-amber-600 hover:bg-warning/20',
                  )}
                  title="Click to have AI diagnose this from the trace + source"
                >
                  {diagnosing === i.key
                    ? <Loader2 size={11} className="animate-spin" />
                    : <Sparkles size={11} />}
                  {i.label}{i.count > 1 ? ` (${i.count}×)` : ''}
                </button>
              ))}
            </div>
            {diagError && (
              <p className="mt-1.5 text-[11px] text-destructive">Diagnosis failed: {diagError}</p>
            )}
            {diagnosis && (
              <div className="mt-2 rounded-lg border border-border bg-muted/30 p-3 text-xs">
                <div className="mb-1 flex items-center gap-2">
                  <Sparkles size={12} className="text-primary" />
                  <span className="font-medium">AI diagnosis</span>
                  <span className={cn(
                    'rounded px-1.5 py-0.5 text-[10px]',
                    diagnosis.risk_level === 'low' ? 'bg-success/10 text-success' : 'bg-warning/10 text-amber-600',
                  )}>
                    {diagnosis.risk_level} risk fix
                  </span>
                  <button
                    onClick={requestFix}
                    disabled={isStreaming}
                    className="ml-auto flex items-center gap-1 rounded-md bg-primary px-2.5 py-1 text-[11px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                    title={isStreaming
                      ? 'A build is already running — wait for it to finish'
                      : 'Send this diagnosis to the builder chat as a fix request (you watch it run)'}
                  >
                    <Wrench size={11} />
                    Fix it
                  </button>
                  <button onClick={() => setDiagnosis(null)}
                          className="text-muted-foreground hover:text-foreground">
                    <X size={12} />
                  </button>
                </div>
                <p className="leading-5">{diagnosis.diagnosis}</p>
                {diagnosis.root_cause && (
                  <p className="mt-1 leading-5 text-muted-foreground">
                    <span className="font-medium text-foreground">Root cause:</span> {diagnosis.root_cause}
                  </p>
                )}
                {diagnosis.files_implicated.length > 0 && (
                  <p className="mt-1 text-muted-foreground">
                    Fix would touch:{' '}
                    {diagnosis.files_implicated.map((f) => (
                      <code key={f.path} className="mr-1.5 rounded bg-muted px-1 py-0.5 text-[10px]">{f.path}</code>
                    ))}
                    — ask for it in the builder chat.
                  </p>
                )}
              </div>
            )}
          </div>
        )}

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
      </>
  )

  if (variant === 'panel') {
    return <div className="flex h-full flex-col overflow-hidden bg-card">{content}</div>
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="flex max-h-[85vh] w-[720px] flex-col rounded-xl border border-border bg-card shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {content}
      </div>
    </div>
  )
}
