import { useEffect, useMemo, useState } from 'react'
import { Database, Loader2, Plus, Table as TableIcon, Trash2, X } from 'lucide-react'
import { apiClient } from '@/api/client'
import { cn } from '@/lib/utils'
import type { Dataset } from '@/types'

/**
 * AppDataPanel — Lists datasets bound to the current app, plus an "Attach"
 * sheet for browsing/binding datasets the user can see.
 *
 * Bound datasets are what the AI builder can wire up (via `useDataset(id)`)
 * and what the runtime proxy will allow at execute time.
 */
export function AppDataPanel({ appId }: { appId: string }) {
  const [bound, setBound] = useState<Dataset[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [sheetOpen, setSheetOpen] = useState(false)

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await apiClient.get<Dataset[]>(`/apps/${appId}/datasets`)
      setBound(data)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(msg)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appId])

  const unbind = async (datasetId: string) => {
    if (!confirm('Detach this dataset from the app? The app will start returning 403 from this dataset.')) return
    await apiClient.delete(`/apps/${appId}/datasets/${datasetId}`)
    refresh()
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold">Data</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Datasets this app can call at runtime via <code>useDataset()</code>.
          </p>
        </div>
        <button
          onClick={() => setSheetOpen(true)}
          className="flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
        >
          <Plus size={14} /> Attach
        </button>
      </div>

      <div className="flex-1 overflow-auto p-3">
        {loading ? (
          <div className="flex justify-center py-6">
            <Loader2 size={18} className="animate-spin text-muted-foreground" />
          </div>
        ) : error ? (
          <div className="rounded bg-red-500/10 px-3 py-2 text-xs text-red-400">{error}</div>
        ) : !bound || bound.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border p-6 text-center">
            <Database size={20} className="mx-auto text-muted-foreground" />
            <p className="mt-2 text-xs text-muted-foreground">
              No datasets attached. Click "Attach" to pick one.
            </p>
          </div>
        ) : (
          <ul className="space-y-2">
            {bound.map((d) => (
              <li key={d.id} className="rounded-lg border border-border bg-background p-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                        {d.kind}
                      </span>
                      <h3 className="truncate text-sm font-medium">{d.name}</h3>
                    </div>
                    {d.description && (
                      <p className="mt-1 text-xs text-muted-foreground">{d.description}</p>
                    )}
                    <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                      useDataset(<span className="text-primary">'{d.id}'</span>)
                    </p>
                  </div>
                  <button
                    onClick={() => unbind(d.id)}
                    className="rounded-lg p-1.5 text-muted-foreground hover:bg-red-500/10 hover:text-red-400"
                    title="Detach"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {sheetOpen && (
        <AttachSheet
          appId={appId}
          boundIds={new Set((bound || []).map((d) => d.id))}
          onClose={() => setSheetOpen(false)}
          onAttached={() => {
            refresh()
          }}
        />
      )}
    </div>
  )
}

// --- Attach sheet ---------------------------------------------------------

function AttachSheet({
  appId,
  boundIds,
  onClose,
  onAttached,
}: {
  appId: string
  boundIds: Set<string>
  onClose: () => void
  onAttached: () => void
}) {
  const [available, setAvailable] = useState<Dataset[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const [attaching, setAttaching] = useState<string | null>(null)

  useEffect(() => {
    let cancel = false
    apiClient
      .get<Dataset[]>('/datasets/discoverable')
      .then((d) => {
        if (!cancel) setAvailable(d)
      })
      .catch((e: unknown) => {
        if (!cancel) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (!cancel) setLoading(false)
      })
    return () => {
      cancel = true
    }
  }, [])

  const filtered = useMemo(() => {
    if (!available) return []
    const q = filter.toLowerCase().trim()
    return available
      .filter((d) => !boundIds.has(d.id))
      .filter(
        (d) =>
          !q ||
          d.name.toLowerCase().includes(q) ||
          (d.description || '').toLowerCase().includes(q),
      )
  }, [available, filter, boundIds])

  const attach = async (datasetId: string) => {
    setAttaching(datasetId)
    try {
      await apiClient.post(`/apps/${appId}/datasets/${datasetId}`)
      onAttached()
      onClose()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setAttaching(null)
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/50 p-4">
      <div className="flex max-h-[80vh] w-full max-w-xl flex-col rounded-lg border border-border bg-card shadow-xl">
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <h2 className="text-sm font-semibold">Attach a dataset</h2>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
            <X size={18} />
          </button>
        </div>

        <div className="border-b border-border px-5 py-3">
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter by name or description…"
            className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
          <p className="mt-2 text-xs text-muted-foreground">
            Only datasets you own or that are shared with the org are shown. To create a new dataset, open Admin → Datasets.
          </p>
        </div>

        <div className="flex-1 overflow-auto px-5 py-3">
          {loading ? (
            <div className="flex justify-center py-8">
              <Loader2 size={20} className="animate-spin text-muted-foreground" />
            </div>
          ) : error ? (
            <div className="rounded bg-red-500/10 px-3 py-2 text-xs text-red-400">{error}</div>
          ) : filtered.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border p-6 text-center">
              <TableIcon size={20} className="mx-auto text-muted-foreground" />
              <p className="mt-2 text-xs text-muted-foreground">
                {available && available.length > 0
                  ? 'No matching datasets. Try a different filter or attach a different one.'
                  : 'No datasets available to attach. Create one in Admin → Datasets.'}
              </p>
            </div>
          ) : (
            <ul className="space-y-2">
              {filtered.map((d) => (
                <li
                  key={d.id}
                  className={cn(
                    'flex items-center justify-between gap-3 rounded-lg border border-border bg-background p-3',
                  )}
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                        {d.kind}
                      </span>
                      <h3 className="truncate text-sm font-medium">{d.name}</h3>
                      <span className="rounded bg-accent/40 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                        {d.visibility}
                      </span>
                    </div>
                    {d.description && (
                      <p className="mt-1 text-xs text-muted-foreground">{d.description}</p>
                    )}
                  </div>
                  <button
                    onClick={() => attach(d.id)}
                    disabled={attaching === d.id}
                    className="shrink-0 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                  >
                    {attaching === d.id ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      'Attach'
                    )}
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
