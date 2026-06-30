/**
 * Build Traces — full traceability for app generation.
 *
 * Lists every generation run for the app, and drills into one to show exactly what
 * the AI did: the user's prompt, the precise system prompts that were sent, and a
 * step-by-step timeline (context → generate → verify → fix → done/error) including
 * the concrete errors at each verify pass — so you can see where it went wrong.
 */
import { useEffect, useState } from 'react'
import {
  X, Loader2, ChevronLeft, ChevronDown, ChevronRight, Database, Sparkles,
  ShieldCheck, RefreshCw, CheckCircle2, AlertTriangle, FileCode, ScrollText,
} from 'lucide-react'
import { apiClient } from '@/api/client'
import { cn } from '@/lib/utils'

interface TraceSummary {
  id: string; user_message: string; model: string; provider: string; status: string
  summary: string; iterations: number; duration_seconds: number
  created_at: string | null; files_changed_count: number
}
interface TraceStep { type: string; [k: string]: any }
interface TraceDetail extends TraceSummary {
  system_prompts: string[]; steps: TraceStep[]
  files_changed: { path: string; action: string }[]; verify: any
}

const STATUS_STYLE: Record<string, string> = {
  passed: 'bg-success/15 text-success',
  failed: 'bg-warning/15 text-warning',
  error: 'bg-destructive/15 text-destructive',
  no_verify: 'bg-muted text-muted-foreground',
  no_files: 'bg-muted text-muted-foreground',
  running: 'bg-primary/15 text-primary',
}

function StatusBadge({ status }: { status: string }) {
  return (
    <span className={cn('rounded-full px-2 py-0.5 text-[10px] font-medium', STATUS_STYLE[status] || 'bg-muted text-muted-foreground')}>
      {status}
    </span>
  )
}

export function TracesModal({ appId, onClose }: { appId: string; onClose: () => void }) {
  const [list, setList] = useState<TraceSummary[] | null>(null)
  const [sel, setSel] = useState<TraceDetail | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)

  useEffect(() => {
    apiClient.get<{ traces: TraceSummary[] }>(`/apps/${appId}/traces`)
      .then((r) => setList(r.traces)).catch(() => setList([]))
  }, [appId])

  const open = async (id: string) => {
    setLoadingDetail(true)
    try {
      setSel(await apiClient.get<TraceDetail>(`/apps/${appId}/traces/${id}`))
    } catch {
      // ignore — stay on list
    } finally {
      setLoadingDetail(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="flex h-[85vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            {sel && (
              <button onClick={() => setSel(null)} className="text-muted-foreground hover:text-foreground">
                <ChevronLeft size={16} />
              </button>
            )}
            <ScrollText size={16} className="text-primary" />
            {sel ? 'Build trace' : 'Build traces'}
          </h3>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground"><X size={16} /></button>
        </div>

        <div className="flex-1 overflow-y-auto">
          {loadingDetail && <div className="flex justify-center py-10"><Loader2 className="animate-spin text-muted-foreground" /></div>}
          {!loadingDetail && !sel && <TraceList list={list} onOpen={open} />}
          {!loadingDetail && sel && <TraceDetailView detail={sel} />}
        </div>
      </div>
    </div>
  )
}

function TraceList({ list, onOpen }: { list: TraceSummary[] | null; onOpen: (id: string) => void }) {
  if (!list) return <div className="flex justify-center py-10"><Loader2 className="animate-spin text-muted-foreground" /></div>
  if (list.length === 0) {
    return <p className="px-4 py-8 text-center text-sm text-muted-foreground">No build traces yet. Generate or modify the app and each run will be recorded here.</p>
  }
  return (
    <ul className="divide-y divide-border">
      {list.map((t) => (
        <li key={t.id}>
          <button onClick={() => onOpen(t.id)} className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-accent/50">
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm">{t.user_message || '(no prompt)'}</div>
              <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted-foreground">
                <span>{t.created_at ? new Date(t.created_at).toLocaleString() : ''}</span>
                <span>· {t.model}</span>
                <span>· {t.files_changed_count} file{t.files_changed_count !== 1 ? 's' : ''}</span>
                {t.iterations > 0 && <span>· {t.iterations} fix{t.iterations !== 1 ? 'es' : ''}</span>}
                <span>· {t.duration_seconds}s</span>
              </div>
            </div>
            <StatusBadge status={t.status} />
            <ChevronRight size={14} className="shrink-0 text-muted-foreground" />
          </button>
        </li>
      ))}
    </ul>
  )
}

const STEP_ICON: Record<string, React.ComponentType<{ size?: number; className?: string }>> = {
  context: Database, generate: Sparkles, verify: ShieldCheck, fix: RefreshCw,
  done: CheckCircle2, error: AlertTriangle,
}

function TraceDetailView({ detail }: { detail: TraceDetail }) {
  const [showPrompts, setShowPrompts] = useState(false)
  return (
    <div className="space-y-4 p-4">
      {/* Summary */}
      <div className="rounded-lg border border-border bg-background p-3">
        <div className="mb-1 flex items-center gap-2">
          <StatusBadge status={detail.status} />
          <span className="text-xs text-muted-foreground">{detail.model} · {detail.duration_seconds}s</span>
        </div>
        <div className="text-sm font-medium">User prompt</div>
        <p className="mt-0.5 whitespace-pre-wrap text-xs text-muted-foreground">{detail.user_message || '(none)'}</p>
        {detail.summary && <p className="mt-2 text-xs">{detail.summary}</p>}
      </div>

      {/* System prompts */}
      <div className="rounded-lg border border-border">
        <button onClick={() => setShowPrompts((v) => !v)} className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium">
          {showPrompts ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          System prompts sent to the model ({detail.system_prompts.length})
        </button>
        {showPrompts && (
          <div className="space-y-2 border-t border-border p-3">
            {detail.system_prompts.map((p, i) => (
              <pre key={i} className="max-h-48 overflow-auto whitespace-pre-wrap rounded bg-muted/40 p-2 font-mono text-[10px] text-muted-foreground">
                {p}
              </pre>
            ))}
          </div>
        )}
      </div>

      {/* Step timeline */}
      <div>
        <div className="mb-2 text-xs font-medium text-muted-foreground">Steps</div>
        <ol className="space-y-2">
          {detail.steps.map((s, i) => {
            const Icon = STEP_ICON[s.type] ?? Sparkles
            const failed = s.type === 'verify' && s.passed === false
            const ok = s.type === 'verify' && s.passed === true
            return (
              <li key={i} className="flex gap-2">
                <div className={cn(
                  'mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full',
                  failed ? 'bg-warning/15 text-warning' : ok ? 'bg-success/15 text-success' : 'bg-primary/10 text-primary',
                )}>
                  <Icon size={13} />
                </div>
                <div className="min-w-0 flex-1 rounded-lg border border-border bg-background px-3 py-2">
                  <StepBody step={s} />
                </div>
              </li>
            )
          })}
        </ol>
      </div>

      {/* Files changed */}
      {detail.files_changed.length > 0 && (
        <div>
          <div className="mb-1 text-xs font-medium text-muted-foreground">Files changed</div>
          <div className="flex flex-wrap gap-1">
            {detail.files_changed.map((f, i) => (
              <span key={i} className="flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">
                <FileCode size={10} /> {f.path}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function StepBody({ step }: { step: TraceStep }) {
  switch (step.type) {
    case 'context':
      return <div className="text-xs">Gathered context — <span className="text-muted-foreground">{step.system_prompt_count} system prompts, {step.message_count} messages, model {step.model}</span></div>
    case 'generate':
      return <div className="text-xs">Generated code — <span className="text-muted-foreground">{(step.files || []).length} files{step.description ? `: ${step.description}` : ''}</span></div>
    case 'verify':
      return (
        <div className="text-xs">
          <span className="font-medium">Verify {step.iteration === 0 ? '(initial)' : `(fix ${step.iteration})`}</span>{' '}
          {step.passed ? <span className="text-success">passed</span> : <span className="text-warning">failed at {step.stage}</span>}
          {step.summary && <span className="text-muted-foreground"> — {step.summary}</span>}
          {Array.isArray(step.errors) && step.errors.length > 0 && (
            <ul className="mt-1 space-y-0.5">
              {step.errors.slice(0, 5).map((e: string, i: number) => (
                <li key={i} className="rounded bg-muted/50 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">{e}</li>
              ))}
            </ul>
          )}
        </div>
      )
    case 'fix':
      return <div className="text-xs">Applied AI fix — <span className="text-muted-foreground">{(step.files || []).length} files</span></div>
    case 'done':
      return <div className="text-xs">Done — <span className="text-muted-foreground">{step.files_changed} files changed</span></div>
    case 'error':
      return <div className="text-xs text-destructive">Error — {step.message}</div>
    default:
      return <div className="text-xs text-muted-foreground">{step.type}</div>
  }
}
