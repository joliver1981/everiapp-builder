import { useEffect, useState } from 'react'
import { X, FilePlus2, FileMinus2, FileDiff, Loader2 } from 'lucide-react'
import { apiClient } from '@/api/client'
import { cn } from '@/lib/utils'

export interface DiffFile {
  path: string
  status: 'added' | 'removed' | 'modified'
  binary: boolean
  additions: number
  deletions: number
  diff: string
  truncated?: boolean
}

export interface VersionDiff {
  app_id: string
  from: string
  to: string
  summary: { added: number; removed: number; modified: number }
  files: DiffFile[]
}

const STATUS_ICON = {
  added: <FilePlus2 size={13} className="text-emerald-500" />,
  removed: <FileMinus2 size={13} className="text-red-500" />,
  modified: <FileDiff size={13} className="text-amber-500" />,
}

function DiffLines({ diff }: { diff: string }) {
  // Render a unified diff with +/- line coloring. Skip the @@/+++/--- noise lines.
  const lines = diff.split('\n')
  return (
    <pre className="overflow-x-auto bg-zinc-950 p-3 text-[11px] leading-relaxed">
      {lines.map((ln, i) => {
        const isAdd = ln.startsWith('+') && !ln.startsWith('+++')
        const isDel = ln.startsWith('-') && !ln.startsWith('---')
        const isHunk = ln.startsWith('@@')
        return (
          <div
            key={i}
            className={cn(
              'whitespace-pre font-mono',
              isAdd && 'bg-emerald-950/40 text-emerald-300',
              isDel && 'bg-red-950/40 text-red-300',
              isHunk && 'text-sky-400',
              !isAdd && !isDel && !isHunk && 'text-zinc-500'
            )}
          >
            {ln || ' '}
          </div>
        )
      })}
    </pre>
  )
}

export function VersionDiffModal({
  appId,
  fromRef,
  toRef,
  onClose,
}: {
  appId: string
  fromRef: string
  toRef: string
  onClose: () => void
}) {
  const [diff, setDiff] = useState<VersionDiff | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [openFile, setOpenFile] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setDiff(null)
    setError(null)
    apiClient
      .get<VersionDiff>(`/apps/${appId}/versions/diff?from=${fromRef}&to=${toRef}`)
      .then((d) => {
        if (cancelled) return
        setDiff(d)
        // Auto-open the first modified file so the user sees content immediately.
        const firstModified = d.files.find((f) => f.status === 'modified' && f.diff)
        setOpenFile(firstModified?.path ?? null)
      })
      .catch((e) => !cancelled && setError(e?.message || 'Failed to load diff'))
    return () => {
      cancelled = true
    }
  }, [appId, fromRef, toRef])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="flex items-center gap-3">
            <FileDiff size={16} className="text-primary" />
            <h3 className="text-sm font-semibold">
              Changes from{' '}
              <code className="rounded bg-muted px-1">{fromRef === 'draft' ? 'draft' : `v${fromRef}`}</code>{' '}
              to{' '}
              <code className="rounded bg-muted px-1">{toRef === 'draft' ? 'draft' : `v${toRef}`}</code>
            </h3>
            {diff && (
              <span className="text-xs text-muted-foreground">
                <span className="text-emerald-500">+{diff.summary.added}</span>{' '}
                <span className="text-red-500">−{diff.summary.removed}</span>{' '}
                <span className="text-amber-500">~{diff.summary.modified}</span>
              </span>
            )}
          </div>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
            <X size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto">
          {error && <div className="p-6 text-center text-sm text-red-400">{error}</div>}
          {!diff && !error && (
            <div className="flex items-center justify-center p-10 text-muted-foreground">
              <Loader2 size={20} className="animate-spin" />
            </div>
          )}
          {diff && diff.files.length === 0 && (
            <div className="p-10 text-center text-sm text-muted-foreground">
              No differences — these versions are identical.
            </div>
          )}
          {diff &&
            diff.files.map((f) => (
              <div key={f.path} className="border-b border-border">
                <button
                  onClick={() => setOpenFile(openFile === f.path ? null : f.path)}
                  className="flex w-full items-center justify-between px-4 py-2 text-left hover:bg-accent/50"
                >
                  <span className="flex items-center gap-2 text-xs">
                    {STATUS_ICON[f.status]}
                    <span className="font-mono">{f.path}</span>
                    {f.binary && (
                      <span className="rounded bg-muted px-1 text-[10px] text-muted-foreground">
                        binary
                      </span>
                    )}
                  </span>
                  <span className="text-[11px] text-muted-foreground">
                    {f.additions > 0 && <span className="text-emerald-500">+{f.additions} </span>}
                    {f.deletions > 0 && <span className="text-red-500">−{f.deletions}</span>}
                  </span>
                </button>
                {openFile === f.path && f.diff && <DiffLines diff={f.diff} />}
                {openFile === f.path && !f.diff && !f.binary && (
                  <div className="px-4 py-2 text-[11px] text-muted-foreground">
                    {f.status === 'added' ? 'New file.' : f.status === 'removed' ? 'File deleted.' : 'No textual changes.'}
                  </div>
                )}
                {f.truncated && (
                  <div className="px-4 py-1 text-[10px] text-amber-500/80">Diff truncated (large file).</div>
                )}
              </div>
            ))}
        </div>
      </div>
    </div>
  )
}
