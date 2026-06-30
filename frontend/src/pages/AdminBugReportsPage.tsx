import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  Bug,
  CheckCircle2,
  ChevronRight,
  Code2,
  FileText,
  Image as ImageIcon,
  Loader2,
  RefreshCw,
  Sparkles,
  X,
  XCircle,
} from 'lucide-react'

import { PageHeader } from '@/components/layout/PageHeader'
import { useBugReportsStore } from '@/stores/bugReportsStore'
import type {
  BugAnalysis,
  BugReportDetail,
  BugReportStatus,
  BugRiskLevel,
  ProposedFile,
} from '@/types'
import { cn } from '@/lib/utils'

const STATUS_BADGE: Record<BugReportStatus, { label: string; cls: string }> = {
  new:        { label: 'New',        cls: 'bg-muted text-muted-foreground' },
  analyzing:  { label: 'Analyzing',  cls: 'bg-warning/10 text-warning' },
  analyzed:   { label: 'Awaiting review', cls: 'bg-primary/10 text-primary' },
  approved:   { label: 'Approved',   cls: 'bg-primary/10 text-primary' },
  applying:   { label: 'Applying',   cls: 'bg-warning/10 text-warning' },
  testing:    { label: 'Testing',    cls: 'bg-warning/10 text-warning' },
  deploying:  { label: 'Deploying',  cls: 'bg-warning/10 text-warning' },
  resolved:   { label: 'Resolved',   cls: 'bg-success/10 text-success' },
  rejected:   { label: 'Rejected',   cls: 'bg-muted text-muted-foreground' },
  failed:     { label: 'Failed',     cls: 'bg-destructive/10 text-destructive' },
}

const RISK_BADGE: Record<BugRiskLevel, string> = {
  low:    'bg-success/10 text-success',
  medium: 'bg-warning/10 text-warning',
  high:   'bg-destructive/10 text-destructive',
}

export function AdminBugReportsPage() {
  const { summaries, isLoadingList, fetchList, fetchDetail, details, approve, reject, reanalyze } =
    useBugReportsStore()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [filter, setFilter] = useState<'all' | 'open' | 'resolved'>('open')
  const [acting, setActing] = useState<'approve' | 'reject' | 'reanalyze' | null>(null)
  const [rejectNote, setRejectNote] = useState('')
  const [reanalyzeNote, setReanalyzeNote] = useState('')

  useEffect(() => { fetchList() }, [fetchList])

  // Refresh selected report while it's mid-flight
  useEffect(() => {
    if (!selectedId) return
    const detail = details[selectedId]
    if (!detail) return
    const inFlight = ['analyzing', 'applying', 'testing', 'deploying'].includes(detail.status)
    if (!inFlight) return
    const id = setInterval(() => fetchDetail(selectedId), 3000)
    return () => clearInterval(id)
  }, [selectedId, details, fetchDetail])

  const visible = useMemo(() => {
    if (filter === 'all') return summaries
    if (filter === 'resolved') return summaries.filter((r) => r.status === 'resolved')
    return summaries.filter((r) => !['resolved', 'rejected'].includes(r.status))
  }, [summaries, filter])

  const detail = selectedId ? details[selectedId] : null
  const latestAnalysis: BugAnalysis | null = detail?.analyses?.[0] || null

  const onSelect = async (id: string) => {
    setSelectedId(id)
    setRejectNote('')
    setReanalyzeNote('')
    if (!details[id]) await fetchDetail(id)
  }

  const onApprove = async () => {
    if (!detail || !latestAnalysis) return
    setActing('approve')
    try {
      await approve(detail.id, latestAnalysis.id)
    } finally { setActing(null) }
  }

  const onReject = async () => {
    if (!detail) return
    setActing('reject')
    try {
      await reject(detail.id, rejectNote.trim())
      setRejectNote('')
    } finally { setActing(null) }
  }

  const onReanalyze = async () => {
    if (!detail) return
    setActing('reanalyze')
    try {
      await reanalyze(detail.id, reanalyzeNote.trim())
      setReanalyzeNote('')
    } finally { setActing(null) }
  }

  return (
    <div>
      <PageHeader
        title="Bug Reports"
        description="Reports submitted from deployed apps. AI analyzes each one and suggests a fix."
        actions={
          <button
            onClick={() => fetchList()}
            className="flex items-center gap-2 rounded-lg bg-muted px-3 py-2 text-sm text-muted-foreground hover:text-foreground"
          >
            <RefreshCw size={14} className={cn(isLoadingList && 'animate-spin')} />
            Refresh
          </button>
        }
      />

      <div className="flex h-[calc(100vh-9rem)]">
        {/* List */}
        <div className="flex w-[420px] shrink-0 flex-col border-r border-border">
          <div className="flex gap-1 border-b border-border px-4 py-2">
            {(['open', 'all', 'resolved'] as const).map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={cn(
                  'rounded-md px-2 py-1 text-xs font-medium capitalize',
                  filter === f
                    ? 'bg-primary/10 text-primary'
                    : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {f}
              </button>
            ))}
            <span className="ml-auto self-center text-xs text-muted-foreground">
              {visible.length} report{visible.length === 1 ? '' : 's'}
            </span>
          </div>

          <div className="flex-1 overflow-auto">
            {isLoadingList && summaries.length === 0 ? (
              <div className="flex justify-center py-10">
                <Loader2 size={20} className="animate-spin text-muted-foreground" />
              </div>
            ) : visible.length === 0 ? (
              <div className="px-4 py-12 text-center">
                <Bug size={32} className="mx-auto text-muted-foreground/30" />
                <p className="mt-2 text-sm text-muted-foreground">No bug reports here.</p>
              </div>
            ) : (
              <div>
                {visible.map((r) => {
                  const badge = STATUS_BADGE[r.status]
                  const selected = r.id === selectedId
                  return (
                    <button
                      key={r.id}
                      onClick={() => onSelect(r.id)}
                      className={cn(
                        'flex w-full items-start gap-3 border-b border-border px-4 py-3 text-left transition-colors',
                        selected ? 'bg-primary/5' : 'hover:bg-accent',
                      )}
                    >
                      <Bug size={14} className="mt-1 shrink-0 text-muted-foreground" />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="truncate text-sm font-medium">{r.title}</span>
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px]">
                          <span className={cn('rounded px-1.5 py-0.5', badge.cls)}>{badge.label}</span>
                          {r.risk_level && (
                            <span className={cn('rounded px-1.5 py-0.5 capitalize', RISK_BADGE[r.risk_level])}>
                              {r.risk_level} risk
                            </span>
                          )}
                          {r.app_name && (
                            <span className="truncate text-muted-foreground">{r.app_name}</span>
                          )}
                          {r.version != null && (
                            <span className="text-muted-foreground">v{r.version}</span>
                          )}
                        </div>
                        <div className="mt-1 text-[10px] text-muted-foreground">
                          {new Date(r.created_at).toLocaleString()}
                          {r.reporter_label ? ` · ${r.reporter_label}` : ''}
                        </div>
                      </div>
                      <ChevronRight size={14} className="mt-1 shrink-0 text-muted-foreground" />
                    </button>
                  )
                })}
              </div>
            )}
          </div>
        </div>

        {/* Detail */}
        <div className="flex flex-1 flex-col overflow-hidden">
          {!detail ? (
            <div className="flex flex-1 items-center justify-center">
              <p className="text-sm text-muted-foreground">Pick a report to view its analysis.</p>
            </div>
          ) : (
            <DetailView
              detail={detail}
              latestAnalysis={latestAnalysis}
              acting={acting}
              rejectNote={rejectNote}
              setRejectNote={setRejectNote}
              reanalyzeNote={reanalyzeNote}
              setReanalyzeNote={setReanalyzeNote}
              onApprove={onApprove}
              onReject={onReject}
              onReanalyze={onReanalyze}
              onClose={() => setSelectedId(null)}
            />
          )}
        </div>
      </div>
    </div>
  )
}

function DetailView({
  detail,
  latestAnalysis,
  acting,
  rejectNote,
  setRejectNote,
  reanalyzeNote,
  setReanalyzeNote,
  onApprove,
  onReject,
  onReanalyze,
  onClose,
}: {
  detail: BugReportDetail
  latestAnalysis: BugAnalysis | null
  acting: 'approve' | 'reject' | 'reanalyze' | null
  rejectNote: string
  setRejectNote: (s: string) => void
  reanalyzeNote: string
  setReanalyzeNote: (s: string) => void
  onApprove: () => void
  onReject: () => void
  onReanalyze: () => void
  onClose: () => void
}) {
  const [openFiles, setOpenFiles] = useState<Record<number, boolean>>({})
  const status = STATUS_BADGE[detail.status]
  const isFinal = ['resolved', 'rejected'].includes(detail.status)
  const canActOnAnalysis = detail.status === 'analyzed' && latestAnalysis && latestAnalysis.proposed_files.length > 0
  const showSpinner = ['analyzing', 'applying', 'testing', 'deploying'].includes(detail.status)

  return (
    <>
      <div className="flex items-start justify-between border-b border-border px-6 py-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className={cn('rounded px-2 py-0.5 text-[10px] font-medium uppercase', status.cls)}>
              {status.label}
            </span>
            {showSpinner && <Loader2 size={12} className="animate-spin text-warning" />}
            {latestAnalysis && (
              <span className={cn('rounded px-2 py-0.5 text-[10px] font-medium uppercase', RISK_BADGE[latestAnalysis.risk_level])}>
                {latestAnalysis.risk_level} risk
              </span>
            )}
          </div>
          <h2 className="mt-1 truncate text-base font-semibold">{detail.title}</h2>
          <div className="mt-1 text-xs text-muted-foreground">
            v{detail.version ?? '?'} · {new Date(detail.created_at).toLocaleString()}
            {detail.reporter_label ? ` · reported by ${detail.reporter_label}` : ''}
          </div>
        </div>
        <button onClick={onClose} className="rounded-lg p-1 text-muted-foreground hover:text-foreground">
          <X size={16} />
        </button>
      </div>

      <div className="flex-1 overflow-auto p-6">
        {detail.error && (
          <div className="mb-4 rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive">
            <strong>Error:</strong> {detail.error}
          </div>
        )}

        {detail.description && (
          <Section title="Description">
            <p className="text-sm whitespace-pre-wrap text-muted-foreground">{detail.description}</p>
          </Section>
        )}

        <CapturedContextBlock detail={detail} />

        {/* AI Analysis */}
        {latestAnalysis ? (
          <Section title={
            <div className="flex items-center gap-2">
              <Sparkles size={14} className="text-primary" />
              <span>AI Analysis</span>
              {latestAnalysis.llm_model && (
                <span className="text-[10px] font-normal text-muted-foreground">{latestAnalysis.llm_model}</span>
              )}
            </div>
          }>
            <SubBlock label="Diagnosis">{latestAnalysis.diagnosis || '—'}</SubBlock>
            <SubBlock label="Likely root cause">{latestAnalysis.root_cause || '—'}</SubBlock>
            <SubBlock label="Risk rationale">
              <div className="flex items-start gap-2">
                <AlertTriangle size={14} className="mt-0.5 shrink-0 text-warning" />
                <span>{latestAnalysis.risk_rationale || '—'}</span>
              </div>
            </SubBlock>

            {latestAnalysis.proposed_files.length > 0 ? (
              <div className="mt-3">
                <h4 className="mb-2 text-xs font-semibold uppercase text-muted-foreground">
                  Proposed file changes ({latestAnalysis.proposed_files.length})
                </h4>
                <div className="space-y-2">
                  {latestAnalysis.proposed_files.map((f, i) => (
                    <ProposedFileBlock
                      key={`${f.path}-${i}`}
                      file={f}
                      open={!!openFiles[i]}
                      onToggle={() => setOpenFiles((s) => ({ ...s, [i]: !s[i] }))}
                    />
                  ))}
                </div>
              </div>
            ) : (
              <p className="mt-3 text-xs text-muted-foreground">
                The AI didn't propose any file changes for this report.
              </p>
            )}
          </Section>
        ) : detail.status === 'analyzing' ? (
          <Section title="AI Analysis">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 size={14} className="animate-spin" />
              Analyzing…
            </div>
          </Section>
        ) : (
          <Section title="AI Analysis">
            <p className="text-sm text-muted-foreground">No analysis yet.</p>
          </Section>
        )}

        {/* Fix attempts */}
        {detail.attempts.length > 0 && (
          <Section title="Fix attempts">
            <div className="space-y-2">
              {detail.attempts.map((a) => (
                <div key={a.id} className="rounded-lg border border-border bg-card p-3 text-xs">
                  <div className="flex items-center justify-between">
                    <span className="font-medium">
                      {a.status === 'succeeded' ? '✓' : a.status === 'failed' ? '✗' : '•'} {a.status}
                      {a.auto_approved && (
                        <span className="ml-2 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
                          auto-approved
                        </span>
                      )}
                    </span>
                    <span className="text-muted-foreground">
                      {new Date(a.created_at).toLocaleString()}
                    </span>
                  </div>
                  <div className="mt-1 text-muted-foreground">
                    base v{a.base_version ?? '?'} → new v{a.new_version ?? '?'}
                    {a.deployment_id && ` · deployment ${a.deployment_id.slice(0, 8)}`}
                  </div>
                  {a.error && (
                    <div className="mt-1 rounded bg-destructive/10 px-2 py-1 text-destructive">
                      {a.error}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </Section>
        )}
      </div>

      {/* Action bar */}
      {!isFinal && (
        <div className="border-t border-border bg-muted/40 p-4">
          {canActOnAnalysis ? (
            <div className="space-y-2">
              <div className="flex items-center justify-end gap-2">
                <button
                  onClick={onReanalyze}
                  disabled={!!acting}
                  className="flex items-center gap-1.5 rounded-lg bg-muted px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
                  title="Run a new analysis pass with optional extra guidance"
                >
                  {acting === 'reanalyze' ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
                  Re-analyze
                </button>
                <button
                  onClick={onReject}
                  disabled={!!acting}
                  className="flex items-center gap-1.5 rounded-lg bg-muted px-3 py-1.5 text-xs text-destructive hover:bg-destructive/10 disabled:opacity-50"
                >
                  {acting === 'reject' ? <Loader2 size={12} className="animate-spin" /> : <XCircle size={12} />}
                  Reject
                </button>
                <button
                  onClick={onApprove}
                  disabled={!!acting}
                  className="flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                >
                  {acting === 'approve' ? <Loader2 size={12} className="animate-spin" /> : <CheckCircle2 size={12} />}
                  Approve & deploy fix
                </button>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <input
                  value={reanalyzeNote}
                  onChange={(e) => setReanalyzeNote(e.target.value)}
                  placeholder="Optional re-analyze note ('focus on the network error')"
                  className="rounded-lg border border-input bg-background px-2 py-1 text-xs"
                />
                <input
                  value={rejectNote}
                  onChange={(e) => setRejectNote(e.target.value)}
                  placeholder="Optional reject reason"
                  className="rounded-lg border border-input bg-background px-2 py-1 text-xs"
                />
              </div>
            </div>
          ) : (
            <div className="text-center text-xs text-muted-foreground">
              {detail.status === 'analyzing' ? 'AI is analyzing — actions will appear when ready.' :
               detail.status === 'failed' ? 'Analysis failed. Try Re-analyze.' :
               'No actions available right now.'}
              {detail.status === 'failed' && (
                <button
                  onClick={onReanalyze}
                  disabled={!!acting}
                  className="ml-2 inline-flex items-center gap-1 rounded-md bg-muted px-2 py-0.5 text-muted-foreground hover:text-foreground disabled:opacity-50"
                >
                  {acting === 'reanalyze' ? <Loader2 size={10} className="animate-spin" /> : <Sparkles size={10} />}
                  Re-analyze
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </>
  )
}

function Section({ title, children }: { title: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="mb-6">
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </h3>
      {children}
    </div>
  )
}

function SubBlock({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-2">
      <div className="text-[11px] font-medium text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-sm">{children}</div>
    </div>
  )
}

function CapturedContextBlock({ detail }: { detail: BugReportDetail }) {
  const ctx = detail.captured_context || {}
  const console = ctx.console_tail || []
  const errors = ctx.network_errors || []
  return (
    <Section title={
      <div className="flex items-center gap-2">
        <FileText size={14} />
        <span>Captured context</span>
      </div>
    }>
      <div className="space-y-1 text-xs">
        {ctx.page_url && (
          <div><span className="text-muted-foreground">URL:</span> <span className="font-mono">{ctx.page_url}</span></div>
        )}
        {ctx.viewport && (
          <div><span className="text-muted-foreground">Viewport:</span> {ctx.viewport.width}×{ctx.viewport.height}</div>
        )}
        {ctx.user_agent && (
          <div><span className="text-muted-foreground">User-agent:</span> <span className="font-mono">{ctx.user_agent}</span></div>
        )}
      </div>

      {detail.screenshot_url && (
        <div className="mt-3">
          <div className="mb-1 flex items-center gap-1 text-[11px] font-medium text-muted-foreground">
            <ImageIcon size={11} /> Screenshot
          </div>
          <a href={detail.screenshot_url} target="_blank" rel="noopener noreferrer">
            <img src={detail.screenshot_url} alt="Bug screenshot" className="max-h-48 rounded-lg border border-border" />
          </a>
        </div>
      )}

      {console.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 text-[11px] font-medium text-muted-foreground">Console tail</div>
          <pre className="max-h-40 overflow-auto rounded-lg bg-black/90 p-2 font-mono text-[11px] text-green-200">
            {console.join('\n')}
          </pre>
        </div>
      )}

      {errors.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 text-[11px] font-medium text-muted-foreground">Network errors</div>
          <div className="space-y-1">
            {errors.map((e, i) => (
              <div key={i} className="rounded bg-destructive/5 px-2 py-1 font-mono text-[11px] text-destructive">
                {e.method} {e.url} → {e.status ?? 'ERR'} {e.error || ''}
              </div>
            ))}
          </div>
        </div>
      )}
    </Section>
  )
}

function ProposedFileBlock({ file, open, onToggle }: { file: ProposedFile; open: boolean; onToggle: () => void }) {
  const ACTION_BADGE: Record<ProposedFile['action'], string> = {
    create: 'bg-success/10 text-success',
    update: 'bg-warning/10 text-warning',
    delete: 'bg-destructive/10 text-destructive',
  }
  return (
    <div className="rounded-lg border border-border bg-card">
      <button
        onClick={onToggle}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs hover:bg-accent"
      >
        <div className="flex min-w-0 items-center gap-2">
          <Code2 size={12} className="shrink-0 text-muted-foreground" />
          <span className="truncate font-mono">{file.path}</span>
        </div>
        <div className="flex items-center gap-2">
          <span className={cn('rounded px-1.5 py-0.5 text-[10px] uppercase', ACTION_BADGE[file.action])}>
            {file.action}
          </span>
          <ChevronRight size={12} className={cn('text-muted-foreground transition-transform', open && 'rotate-90')} />
        </div>
      </button>
      {open && file.action !== 'delete' && (
        <pre className="max-h-96 overflow-auto border-t border-border bg-black/90 p-3 font-mono text-[11px] text-green-100">
          {file.content}
        </pre>
      )}
    </div>
  )
}
