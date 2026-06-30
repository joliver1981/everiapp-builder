import { useEffect, useState } from 'react'
import { X, Loader2, History, RotateCcw } from 'lucide-react'
import { apiClient } from '@/api/client'

interface HistoryEntry {
  seq: number
  taken_at: string | null
  note: string
  message_id: string | null
}

export function RewindModal({ appId, onClose }: { appId: string; onClose: () => void }) {
  const [entries, setEntries] = useState<HistoryEntry[] | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  const load = () =>
    apiClient.get<{ entries: HistoryEntry[] }>(`/apps/${appId}/history`).then((r) => setEntries(r.entries)).catch(() => setEntries([]))
  useEffect(() => { load() }, [appId])

  const rewind = async (seq: number) => {
    if (!confirm(`Rewind the draft to checkpoint #${seq}? Your current state is saved first, so this is undoable.`)) return
    try {
      await apiClient.post(`/apps/${appId}/history/${seq}/restore`)
      setMsg(`Rewound to #${seq}. Reload the editor / preview to see it.`)
      load()
    } catch (e: any) {
      setMsg(`Failed: ${e?.message}`)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div className="flex max-h-[80vh] w-full max-w-lg flex-col overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
           onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h3 className="flex items-center gap-2 text-sm font-semibold"><History size={16} className="text-primary" /> Rewind history</h3>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground"><X size={16} /></button>
        </div>
        <div className="flex-1 overflow-y-auto p-3">
          <p className="mb-2 px-1 text-xs text-muted-foreground">Each checkpoint is the draft state captured just before an AI turn.</p>
          {msg && <p className="mb-2 px-1 text-xs text-green-400">{msg}</p>}
          {!entries && <div className="flex justify-center py-8"><Loader2 className="animate-spin text-muted-foreground" /></div>}
          {entries && entries.length === 0 && <p className="px-1 text-sm text-muted-foreground">No checkpoints yet.</p>}
          {entries && entries.length > 0 && (
            <ul className="space-y-1.5">
              {entries.map((e) => (
                <li key={e.seq} className="flex items-center justify-between rounded-lg border border-border p-2.5">
                  <div className="min-w-0">
                    <div className="truncate text-xs">{e.note || `Checkpoint #${e.seq}`}</div>
                    <div className="text-[10px] text-muted-foreground">{e.taken_at ? new Date(e.taken_at).toLocaleString() : `#${e.seq}`}</div>
                  </div>
                  <button onClick={() => rewind(e.seq)}
                          className="flex items-center gap-1 rounded px-2 py-1 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground">
                    <RotateCcw size={10} /> Rewind
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  )
}
