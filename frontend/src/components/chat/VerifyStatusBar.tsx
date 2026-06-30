/**
 * Live AI self-heal status, shown between the chat transcript and the input box.
 *
 * Two modes:
 *
 *  1. While streaming: progress for the current verification pass (initial check
 *     or a fix-iteration). Spinner + summary.
 *
 *  2. After turn done: outcome from the final verify pass. On red, lists the
 *     errors and exposes the "Roll back to last-known-good" button.
 *
 * Both panels are intentionally compact — the chat is the primary content.
 */
import { useState } from 'react'
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronRight,
  Loader2, RotateCcw, X,
} from 'lucide-react'

import { cn } from '@/lib/utils'
import type { VerifyProgress, VerifyResult } from '@/stores/chatStore'

interface Props {
  progress: VerifyProgress | null
  result: VerifyResult | null
  rollbackAvailable: boolean
  onRollback: () => Promise<{ ok: boolean; error?: string }>
  onDismiss: () => void
}

export function VerifyStatusBar({
  progress, result, rollbackAvailable, onRollback, onDismiss,
}: Props) {
  // Result takes precedence over progress (turn is over).
  if (result) return <ResultBar result={result} rollbackAvailable={rollbackAvailable}
                               onRollback={onRollback} onDismiss={onDismiss} />
  if (progress) return <ProgressBar progress={progress} />
  return null
}

function ProgressBar({ progress }: { progress: VerifyProgress }) {
  const isInitial = progress.iteration === 0
  const label = isInitial
    ? 'Verifying...'
    : `Fixing (try ${progress.iteration}/${progress.max})...`

  const lineSummary = progress.status === 'iteration_done'
    ? progress.passed
      ? `✓ ${progress.summary || 'passed'}`
      : `✗ ${progress.summary || 'failed'}`
    : label

  const ok = progress.passed === true
  const failed = progress.passed === false
  // Show the actual error under the progress line so the user can SEE what's
  // being fixed (instead of an opaque "Fixing 2/8...").
  const firstError = failed && progress.errors && progress.errors.length > 0
    ? progress.errors[0]
    : null

  return (
    <div className={cn(
      'border-t border-border px-4 py-2 text-xs',
      ok ? 'bg-success/5 text-success' :
      failed ? 'bg-warning/5 text-warning' :
      'bg-muted/30 text-muted-foreground',
    )}>
      <div className="flex items-center gap-2">
        {progress.status === 'running'
          ? <Loader2 size={12} className="animate-spin shrink-0" />
          : ok
            ? <CheckCircle2 size={12} className="shrink-0" />
            : <AlertTriangle size={12} className="shrink-0" />}
        <span className="truncate">{lineSummary}</span>
        {progress.duration_seconds !== undefined && (
          <span className="ml-auto shrink-0 text-[10px] opacity-70">
            {progress.duration_seconds.toFixed(1)}s
          </span>
        )}
      </div>
      {firstError && (
        <div className="mt-1 max-h-8 overflow-hidden break-words pl-5 font-mono text-[10px] leading-snug opacity-80">
          {firstError}
        </div>
      )}
    </div>
  )
}

function ResultBar({
  result, rollbackAvailable, onRollback, onDismiss,
}: {
  result: VerifyResult
  rollbackAvailable: boolean
  onRollback: () => Promise<{ ok: boolean; error?: string }>
  onDismiss: () => void
}) {
  const [open, setOpen] = useState(!result.passed)
  const [rollingBack, setRollingBack] = useState(false)
  const [rollbackError, setRollbackError] = useState<string | null>(null)

  const handleRollback = async () => {
    if (!confirm('Restore the draft to its state before this AI turn? This cannot be undone.')) {
      return
    }
    setRollingBack(true)
    setRollbackError(null)
    try {
      const r = await onRollback()
      if (!r.ok) setRollbackError(r.error || 'Rollback failed')
    } finally {
      setRollingBack(false)
    }
  }

  if (result.passed) {
    // Compact "all green" bar with a dismiss after a few seconds — but keep it
    // visible so the user has feedback the loop actually ran.
    return (
      <div className="flex items-center gap-2 border-t border-border bg-success/5 px-4 py-2 text-xs text-success">
        <CheckCircle2 size={12} />
        <span>✓ Verified — {result.summary}</span>
        <span className="ml-auto text-[10px] opacity-70">{result.duration_seconds.toFixed(1)}s</span>
        <button
          onClick={onDismiss}
          className="rounded p-0.5 opacity-50 hover:opacity-100"
          title="Dismiss"
        >
          <X size={12} />
        </button>
      </div>
    )
  }

  // Red path — show errors + rollback button
  return (
    <div className="border-t border-border bg-destructive/5">
      <div className="flex items-center gap-2 px-4 py-2 text-xs">
        <button
          onClick={() => setOpen(!open)}
          className="flex items-center gap-1 text-destructive hover:opacity-80"
        >
          {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          <AlertTriangle size={12} />
          <span className="font-medium">{result.summary}</span>
        </button>
        <span className="text-[10px] text-muted-foreground">
          {result.errors.length} error{result.errors.length !== 1 ? 's' : ''}
          {' · '}{result.duration_seconds.toFixed(1)}s
        </span>
        <div className="ml-auto flex items-center gap-1">
          {rollbackAvailable && (
            <button
              onClick={handleRollback}
              disabled={rollingBack}
              className="flex items-center gap-1 rounded-md bg-card px-2 py-1 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
              title="Restore draft to the state before this AI turn"
            >
              {rollingBack ? <Loader2 size={10} className="animate-spin" /> : <RotateCcw size={10} />}
              Roll back
            </button>
          )}
          <button
            onClick={onDismiss}
            className="rounded p-0.5 text-muted-foreground hover:text-foreground"
            title="Dismiss"
          >
            <X size={12} />
          </button>
        </div>
      </div>

      {open && (
        <div className="max-h-48 space-y-1 overflow-auto border-t border-border bg-card px-4 py-2 text-[11px]">
          {result.errors.slice(0, 20).map((e, i) => (
            <div key={i} className="flex items-start gap-2">
              <span className="shrink-0 rounded bg-muted px-1 py-0.5 text-[9px] uppercase text-muted-foreground">
                {e.stage}
              </span>
              <div className="min-w-0 flex-1">
                {e.file && (
                  <span className="font-mono text-muted-foreground">
                    {e.file}{e.line ? `:${e.line}` : ''}{e.column ? `:${e.column}` : ''}
                    {e.code ? ` [${e.code}]` : ''}:&nbsp;
                  </span>
                )}
                <span className="whitespace-pre-wrap break-words">{e.message.slice(0, 400)}</span>
              </div>
            </div>
          ))}
          {result.errors.length > 20 && (
            <div className="text-muted-foreground/70">
              ... and {result.errors.length - 20} more.
            </div>
          )}
        </div>
      )}

      {rollbackError && (
        <div className="border-t border-border bg-destructive/10 px-4 py-1 text-[11px] text-destructive">
          {rollbackError}
        </div>
      )}
    </div>
  )
}
