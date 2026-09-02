import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  Braces,
  ChevronDown,
  ChevronRight,
  Clock,
  FileCode2,
  Loader2,
  MessageSquare,
  Play,
  Plus,
  RefreshCw,
} from 'lucide-react'
import { apiClient, ApiError } from '@/api/client'
import { cn } from '@/lib/utils'

interface FnInfo {
  name: string
  runtime: string
  path: string
  timeout_s: number
  summary: string
  size_bytes: number
  modified_at: string
  callers: string[]
  in_published: boolean
}

interface CallRow {
  id: string
  fn_name: string
  source: string
  trigger: 'app' | 'panel' | string
  ok: boolean
  status_code: number
  error: string
  duration_ms: number
  args_preview: string
  result_preview: string
  logs: string
  created_at: string | null
}

interface RunResult {
  ok: boolean
  result: unknown
  error?: string
  status_code?: number
  logs: string[]
  duration_ms?: number
}

interface Props {
  appId: string
  /** Open a file in the Code tab (server/functions/<name>.py or a caller). */
  onOpenFile: (path: string) => void
  /** Switch to the chat so the AI can write or change a function. */
  onAskAi: () => void
}

function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    try {
      const parsed = JSON.parse(err.message)
      if (typeof parsed.detail === 'string') return parsed.detail
    } catch { /* not JSON */ }
  }
  return err instanceof Error ? err.message : 'Something went wrong'
}

function pretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function fmtTime(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z')
  return d.toLocaleString()
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  return `${(n / 1024).toFixed(1)} KB`
}

/**
 * The builder's Functions tab: every server function in the draft, what
 * calls it, whether the published version has it, a test-run box, and the
 * recent-call log. Editing stays in the Code tab — "Open in editor" jumps
 * there — so there's still exactly one place source is changed.
 */
export function FunctionsPanel({ appId, onOpenFile, onAskAi }: Props) {
  const [functions, setFunctions] = useState<FnInfo[]>([])
  const [runtimeAvailable, setRuntimeAvailable] = useState(true)
  const [publishedVersion, setPublishedVersion] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedName, setSelectedName] = useState<string | null>(null)

  const [newName, setNewName] = useState('')
  const [isCreating, setIsCreating] = useState(false)

  const [argsText, setArgsText] = useState('{}')
  const [isRunning, setIsRunning] = useState(false)
  const [runResult, setRunResult] = useState<RunResult | null>(null)

  const [calls, setCalls] = useState<CallRow[]>([])
  const [expandedCall, setExpandedCall] = useState<string | null>(null)

  const selected = useMemo(
    () => functions.find((f) => f.name === selectedName) || null,
    [functions, selectedName],
  )

  const load = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const data = await apiClient.get<{
        functions: FnInfo[]; runtime_available: boolean; published_version: number
      }>(`/apps/${appId}/functions`)
      setFunctions(data.functions)
      setRuntimeAvailable(data.runtime_available)
      setPublishedVersion(data.published_version)
      setSelectedName((cur) =>
        cur && data.functions.some((f) => f.name === cur) ? cur : data.functions[0]?.name ?? null,
      )
    } catch (err) {
      setError(describeError(err))
      setFunctions([])
    } finally {
      setIsLoading(false)
    }
  }, [appId])

  const loadCalls = useCallback(async (fnName: string | null) => {
    try {
      const path = fnName
        ? `/apps/${appId}/functions/${encodeURIComponent(fnName)}/calls?limit=25`
        : `/apps/${appId}/functions/calls?limit=25`
      const data = await apiClient.get<{ calls: CallRow[] }>(path)
      setCalls(data.calls)
    } catch {
      setCalls([])
    }
  }, [appId])

  useEffect(() => { load() }, [load])
  useEffect(() => {
    setRunResult(null)
    setExpandedCall(null)
    loadCalls(selectedName)
  }, [selectedName, loadCalls])

  const handleCreate = async () => {
    const name = newName.trim()
    if (!name) return
    setIsCreating(true)
    setError(null)
    try {
      const created = await apiClient.post<{ name: string; path: string }>(`/apps/${appId}/functions`, { name })
      setNewName('')
      await load()
      setSelectedName(created.name)
      onOpenFile(created.path)
    } catch (err) {
      setError(describeError(err))
    } finally {
      setIsCreating(false)
    }
  }

  const handleRun = async () => {
    if (!selected) return
    let args: unknown = null
    const text = argsText.trim()
    if (text) {
      try {
        args = JSON.parse(text)
      } catch {
        setRunResult({ ok: false, result: null, error: 'Args must be valid JSON (e.g. {"a": 1}).', logs: [] })
        return
      }
    }
    setIsRunning(true)
    setRunResult(null)
    try {
      const res = await apiClient.post<RunResult>(
        `/apps/${appId}/functions/${encodeURIComponent(selected.name)}/run`, { args },
      )
      setRunResult(res)
    } catch (err) {
      setRunResult({ ok: false, result: null, error: describeError(err), logs: [] })
    } finally {
      setIsRunning(false)
      loadCalls(selected.name)
    }
  }

  const orphan = (f: FnInfo) => f.callers.length === 0
  const draftOnly = (f: FnInfo) => publishedVersion > 0 && !f.in_published

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <span className="flex items-center gap-1.5 text-xs font-medium">
          <Braces size={14} />
          Server functions
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">{functions.length}</span>
        </span>
        <button
          onClick={load}
          className="rounded-lg p-1 text-muted-foreground transition-colors hover:text-foreground"
          title="Refresh"
        >
          <RefreshCw size={12} className={cn(isLoading && 'animate-spin')} />
        </button>
        <div className="ml-auto flex items-center gap-2">
          <form
            className="flex items-center gap-1"
            onSubmit={(e) => { e.preventDefault(); handleCreate() }}
          >
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value.toLowerCase())}
              placeholder="new-function-name"
              pattern="[a-z][a-z0-9_-]{0,63}"
              className="w-44 rounded-lg border border-input bg-secondary px-2 py-1 font-mono text-xs focus:outline-none focus:ring-2 focus:ring-ring"
            />
            <button
              type="submit"
              disabled={isCreating || !newName.trim()}
              className="flex items-center gap-1 rounded-lg border border-border px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
              title="Scaffold server/functions/<name>.py and open it in the editor"
            >
              {isCreating ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />}
              New
            </button>
          </form>
          <button
            onClick={onAskAi}
            className="flex items-center gap-1 rounded-lg border border-border px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            title="Describe the function you need in the chat and the AI writes it (and the UI that calls it)"
          >
            <MessageSquare size={12} />
            Ask AI
          </button>
        </div>
      </div>

      {!runtimeAvailable && (
        <div className="flex items-center gap-2 border-b border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
          <AlertTriangle size={14} />
          The platform&apos;s Python runtime for server functions isn&apos;t available — functions can be edited but not run.
          Reinstall the platform or set AIHUB_PYTHON_DIR.
        </div>
      )}
      {error && (
        <div className="border-b border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">{error}</div>
      )}

      {isLoading && functions.length === 0 ? (
        <div className="flex flex-1 items-center justify-center">
          <Loader2 size={20} className="animate-spin text-muted-foreground" />
        </div>
      ) : functions.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
          <Braces size={36} className="text-muted-foreground/30" />
          <p className="text-sm font-medium">No server functions yet</p>
          <p className="max-w-md text-xs text-muted-foreground">
            A server function is a Python file at <code className="rounded bg-muted px-1">server/functions/&lt;name&gt;.py</code> that
            runs on the platform with a hard timeout. The app&apos;s UI calls it with{' '}
            <code className="rounded bg-muted px-1">callFunction(&apos;name&apos;, args)</code> — heavy data work, exports,
            and anything that needs pandas belong here rather than in the browser.
          </p>
          <p className="text-xs text-muted-foreground">
            Type a name above and press <strong>New</strong>, or <strong>Ask AI</strong> to have one written for you.
          </p>
        </div>
      ) : (
        <div className="flex flex-1 overflow-hidden">
          {/* Function list */}
          <div className="w-72 shrink-0 overflow-y-auto border-r border-border bg-card">
            {functions.map((f) => (
              <button
                key={f.name}
                onClick={() => setSelectedName(f.name)}
                className={cn(
                  'flex w-full flex-col gap-1 border-b border-border px-3 py-2.5 text-left transition-colors',
                  selectedName === f.name ? 'bg-primary/10' : 'hover:bg-accent',
                )}
              >
                <div className="flex items-center gap-2">
                  <span className="truncate font-mono text-xs font-medium">{f.name}</span>
                  <span className="ml-auto flex shrink-0 items-center gap-0.5 text-[10px] text-muted-foreground" title="Timeout">
                    <Clock size={10} />
                    {f.timeout_s}s
                  </span>
                </div>
                {f.summary && (
                  <p className="line-clamp-1 text-[11px] text-muted-foreground">{f.summary}</p>
                )}
                <div className="flex flex-wrap gap-1">
                  {orphan(f) ? (
                    <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-300" title="No src/ file calls callFunction with this name">
                      not called from UI
                    </span>
                  ) : (
                    <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                      used by {f.callers.length} file{f.callers.length === 1 ? '' : 's'}
                    </span>
                  )}
                  {draftOnly(f) && (
                    <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground" title={`Not in the published version v${publishedVersion} — save a new version to ship it`}>
                      draft only
                    </span>
                  )}
                </div>
              </button>
            ))}
          </div>

          {/* Detail */}
          {selected ? (
            <div className="flex flex-1 flex-col gap-4 overflow-y-auto p-4">
              <div className="flex flex-wrap items-start gap-3">
                <div className="min-w-0 flex-1">
                  <h3 className="font-mono text-sm font-semibold">{selected.name}</h3>
                  {selected.summary && <p className="mt-0.5 text-xs text-muted-foreground">{selected.summary}</p>}
                  <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
                    <span>{selected.runtime}</span>
                    <span>timeout {selected.timeout_s}s</span>
                    <span>{fmtBytes(selected.size_bytes)}</span>
                    <span>modified {fmtTime(selected.modified_at)}</span>
                    <span>
                      {publishedVersion > 0
                        ? selected.in_published ? `in published v${publishedVersion}` : `draft only — not in v${publishedVersion}`
                        : 'never published'}
                    </span>
                  </div>
                </div>
                <button
                  onClick={() => onOpenFile(selected.path)}
                  className="flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
                >
                  <FileCode2 size={12} />
                  Open in editor
                </button>
              </div>

              {/* Callers */}
              <div className="rounded-lg border border-border p-3">
                <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Called from</p>
                {selected.callers.length === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    Nothing in <code className="rounded bg-muted px-1">src/</code> calls{' '}
                    <code className="rounded bg-muted px-1">callFunction(&apos;{selected.name}&apos;)</code> yet.
                  </p>
                ) : (
                  <div className="flex flex-wrap gap-1.5">
                    {selected.callers.map((c) => (
                      <button
                        key={c}
                        onClick={() => onOpenFile(c)}
                        className="rounded bg-muted px-2 py-0.5 font-mono text-[11px] text-muted-foreground transition-colors hover:text-foreground"
                        title="Open in editor"
                      >
                        {c}
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* Test run */}
              <div className="rounded-lg border border-border p-3">
                <div className="mb-2 flex items-center justify-between">
                  <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Test run · draft</p>
                  <button
                    onClick={handleRun}
                    disabled={isRunning || !runtimeAvailable}
                    className="flex items-center gap-1.5 rounded-lg bg-success px-3 py-1 text-xs font-medium text-white transition-colors hover:bg-success/90 disabled:opacity-50"
                  >
                    {isRunning ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
                    Run
                  </button>
                </div>
                <label className="mb-1 block text-[11px] text-muted-foreground">args (JSON) — what the UI would pass as the second argument</label>
                <textarea
                  value={argsText}
                  onChange={(e) => setArgsText(e.target.value)}
                  rows={3}
                  spellCheck={false}
                  className="w-full rounded-lg border border-input bg-secondary px-2 py-1.5 font-mono text-xs focus:outline-none focus:ring-2 focus:ring-ring"
                />
                {runResult && (
                  <div className={cn(
                    'mt-2 rounded-lg p-2 text-xs',
                    runResult.ok ? 'bg-success/10' : 'bg-destructive/10',
                  )}>
                    <div className="flex items-center gap-2">
                      <span className={cn('font-medium', runResult.ok ? 'text-success' : 'text-destructive')}>
                        {runResult.ok ? 'OK' : `Failed${runResult.status_code ? ` (${runResult.status_code})` : ''}`}
                      </span>
                      {typeof runResult.duration_ms === 'number' && (
                        <span className="text-muted-foreground">{runResult.duration_ms} ms</span>
                      )}
                    </div>
                    {!runResult.ok && runResult.error && (
                      <p className="mt-1 whitespace-pre-wrap text-destructive">{runResult.error}</p>
                    )}
                    {runResult.ok && (
                      <pre className="mt-1 max-h-64 overflow-auto rounded bg-background/60 p-2 font-mono text-[11px]">{pretty(runResult.result)}</pre>
                    )}
                    {runResult.logs.length > 0 && (
                      <div className="mt-2">
                        <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Logs</p>
                        <pre className="mt-1 max-h-40 overflow-auto rounded bg-background/60 p-2 font-mono text-[11px] text-muted-foreground">{runResult.logs.join('\n')}</pre>
                      </div>
                    )}
                  </div>
                )}
              </div>

              {/* Recent calls */}
              <div className="rounded-lg border border-border p-3">
                <div className="mb-1.5 flex items-center justify-between">
                  <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Recent calls</p>
                  <button
                    onClick={() => loadCalls(selected.name)}
                    className="rounded p-1 text-muted-foreground transition-colors hover:text-foreground"
                    title="Refresh"
                  >
                    <RefreshCw size={11} />
                  </button>
                </div>
                {calls.length === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    No calls recorded yet. Calls from the running app and test runs from this panel both show up here.
                  </p>
                ) : (
                  <div className="divide-y divide-border">
                    {calls.map((c) => {
                      const open = expandedCall === c.id
                      return (
                        <div key={c.id} className="py-1.5">
                          <button
                            onClick={() => setExpandedCall(open ? null : c.id)}
                            className="flex w-full items-center gap-2 text-left text-xs"
                          >
                            {open ? <ChevronDown size={12} className="shrink-0 text-muted-foreground" /> : <ChevronRight size={12} className="shrink-0 text-muted-foreground" />}
                            <span className={cn('w-10 shrink-0 font-medium', c.ok ? 'text-success' : 'text-destructive')}>
                              {c.ok ? 'ok' : c.status_code}
                            </span>
                            <span className="w-12 shrink-0 rounded bg-muted px-1.5 py-0.5 text-center text-[10px] text-muted-foreground" title={c.trigger === 'panel' ? 'Test run from this panel' : 'Called by the running app'}>
                              {c.trigger}
                            </span>
                            <span className="w-14 shrink-0 text-muted-foreground">{c.duration_ms} ms</span>
                            <span className="w-12 shrink-0 text-muted-foreground">{c.source}</span>
                            <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted-foreground">{c.args_preview || '—'}</span>
                            <span className="shrink-0 text-[10px] text-muted-foreground">{fmtTime(c.created_at)}</span>
                          </button>
                          {open && (
                            <div className="ml-6 mt-1.5 space-y-1.5 text-[11px]">
                              {c.error && <p className="whitespace-pre-wrap text-destructive">{c.error}</p>}
                              {c.args_preview && (
                                <div>
                                  <span className="text-muted-foreground">args: </span>
                                  <code className="break-all font-mono">{c.args_preview}</code>
                                </div>
                              )}
                              {c.result_preview && (
                                <div>
                                  <span className="text-muted-foreground">result: </span>
                                  <code className="break-all font-mono">{c.result_preview}</code>
                                </div>
                              )}
                              {c.logs && (
                                <pre className="max-h-40 overflow-auto rounded bg-muted/50 p-2 font-mono text-muted-foreground">{c.logs}</pre>
                              )}
                            </div>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
              Select a function
            </div>
          )}
        </div>
      )}
    </div>
  )
}
