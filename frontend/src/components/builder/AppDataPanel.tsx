import { useEffect, useMemo, useState } from 'react'
import { Bot, Database, Globe, Loader2, Plus, Table as TableIcon, Trash2, X } from 'lucide-react'
import { apiClient } from '@/api/client'
import type { Dataset } from '@/types'

/**
 * AppDataPanel — what this app can reach at runtime:
 *   - Datasets (admin-defined SQL/REST data) → useDataset(id)
 *   - Connections (app-callable external APIs) → callConnection(id, {...})
 *     AI-provider connections (kind "ai") → aiChat(id, { messages: [...] })
 * Each section lists what's attached plus an "Attach" sheet for adding more.
 * Attaching is what the AI builder can wire up and what the runtime allows.
 */
interface AppConnection {
  id: string
  name: string
  description: string
  base_url: string
  app_callable?: boolean
  kind?: string
  // kind === 'ai' only:
  provider?: string
  models?: string[]
  default_model?: string | null
}

export function AppDataPanel({ appId }: { appId: string }) {
  // --- Datasets ---
  const [datasets, setDatasets] = useState<Dataset[] | null>(null)
  const [dsLoading, setDsLoading] = useState(true)
  const [dsError, setDsError] = useState<string | null>(null)
  const [dsSheet, setDsSheet] = useState(false)

  // --- Connections ---
  const [conns, setConns] = useState<AppConnection[] | null>(null)
  const [connLoading, setConnLoading] = useState(true)
  const [connError, setConnError] = useState<string | null>(null)
  const [connSheet, setConnSheet] = useState(false)

  const refreshDatasets = async () => {
    setDsLoading(true)
    setDsError(null)
    try {
      setDatasets(await apiClient.get<Dataset[]>(`/apps/${appId}/datasets`))
    } catch (e: unknown) {
      setDsError(e instanceof Error ? e.message : String(e))
    } finally {
      setDsLoading(false)
    }
  }

  const refreshConns = async () => {
    setConnLoading(true)
    setConnError(null)
    try {
      setConns(await apiClient.get<AppConnection[]>(`/apps/${appId}/connections`))
    } catch (e: unknown) {
      setConnError(e instanceof Error ? e.message : String(e))
    } finally {
      setConnLoading(false)
    }
  }

  useEffect(() => {
    refreshDatasets()
    refreshConns()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appId])

  const unbindDataset = async (id: string) => {
    if (!confirm('Detach this dataset? The app will start returning 403 from it.')) return
    await apiClient.delete(`/apps/${appId}/datasets/${id}`)
    refreshDatasets()
  }

  const unbindConn = async (id: string) => {
    if (!confirm('Detach this connection? callConnection() to it will start returning 403.')) return
    await apiClient.delete(`/apps/${appId}/connections/${id}`)
    refreshConns()
  }

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold">Data &amp; APIs</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          External data and services this app can use at runtime.
        </p>
      </div>

      <div className="flex-1 space-y-6 overflow-auto p-3">
        {/* --- Datasets --- */}
        <section>
          <SectionHead
            title="Datasets"
            hint={
              <>
                Live data via <code>useDataset()</code>.
              </>
            }
            onAttach={() => setDsSheet(true)}
          />
          {dsLoading ? (
            <Spinner />
          ) : dsError ? (
            <ErrBox>{dsError}</ErrBox>
          ) : !datasets || datasets.length === 0 ? (
            <Empty icon={<Database size={18} className="mx-auto text-muted-foreground" />}>
              No datasets attached. Click "Attach" to pick one.
            </Empty>
          ) : (
            <ul className="space-y-2">
              {datasets.map((d) => (
                <li key={d.id} className="rounded-lg border border-border bg-background p-3">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <Badge>{d.kind}</Badge>
                        <h3 className="truncate text-sm font-medium">{d.name}</h3>
                      </div>
                      {d.description && (
                        <p className="mt-1 text-xs text-muted-foreground">{d.description}</p>
                      )}
                      <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                        useDataset(<span className="text-primary">'{d.id}'</span>)
                      </p>
                    </div>
                    <DetachButton onClick={() => unbindDataset(d.id)} />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* --- Connections --- */}
        <section>
          <SectionHead
            title="Connections"
            hint={
              <>
                External APIs via <code>callConnection()</code>, AI via <code>aiChat()</code>.
              </>
            }
            onAttach={() => setConnSheet(true)}
          />
          {connLoading ? (
            <Spinner />
          ) : connError ? (
            <ErrBox>{connError}</ErrBox>
          ) : !conns || conns.length === 0 ? (
            <Empty icon={<Globe size={18} className="mx-auto text-muted-foreground" />}>
              No connections attached. Attach an app-callable connection to let the app call an
              external API or LLM provider.
            </Empty>
          ) : (
            <ul className="space-y-2">
              {conns.map((c) => {
                const isAI = c.kind === 'ai'
                return (
                  <li key={c.id} className="rounded-lg border border-border bg-background p-3">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          {isAI ? (
                            <Bot size={13} className="shrink-0 text-muted-foreground" />
                          ) : (
                            <Globe size={13} className="shrink-0 text-muted-foreground" />
                          )}
                          <h3 className="truncate text-sm font-medium">{c.name}</h3>
                          {isAI && <Badge>AI</Badge>}
                          {isAI && c.provider && (
                            <span className="truncate text-[11px] text-muted-foreground">{c.provider}</span>
                          )}
                        </div>
                        {isAI ? (
                          <p className="mt-1 truncate text-[11px] text-muted-foreground">
                            {c.models?.length ?? 0} model{(c.models?.length ?? 0) === 1 ? '' : 's'}
                            {c.default_model ? ` · default ${c.default_model}` : ''}
                          </p>
                        ) : (
                          c.base_url && (
                            <p className="mt-1 truncate font-mono text-[11px] text-muted-foreground">{c.base_url}</p>
                          )
                        )}
                        <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                          {isAI ? (
                            <>aiChat(<span className="text-primary">'{c.id}'</span>, {'{ messages: [...] }'})</>
                          ) : (
                            <>callConnection(<span className="text-primary">'{c.id}'</span>, …)</>
                          )}
                        </p>
                      </div>
                      <DetachButton onClick={() => unbindConn(c.id)} />
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </section>
      </div>

      {dsSheet && (
        <AttachDatasetSheet
          appId={appId}
          boundIds={new Set((datasets || []).map((d) => d.id))}
          onClose={() => setDsSheet(false)}
          onAttached={refreshDatasets}
        />
      )}
      {connSheet && (
        <AttachConnectionSheet
          appId={appId}
          boundIds={new Set((conns || []).map((c) => c.id))}
          onClose={() => setConnSheet(false)}
          onAttached={refreshConns}
        />
      )}
    </div>
  )
}

// --- small shared bits ------------------------------------------------------

function SectionHead({ title, hint, onAttach }: { title: string; hint: React.ReactNode; onAttach: () => void }) {
  return (
    <div className="mb-2 flex items-center justify-between">
      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h3>
        <p className="mt-0.5 text-[11px] text-muted-foreground">{hint}</p>
      </div>
      <button
        onClick={onAttach}
        className="flex items-center gap-1.5 rounded-lg bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground hover:bg-primary/90"
      >
        <Plus size={13} /> Attach
      </button>
    </div>
  )
}

const Spinner = () => (
  <div className="flex justify-center py-4">
    <Loader2 size={18} className="animate-spin text-muted-foreground" />
  </div>
)
const ErrBox = ({ children }: { children: React.ReactNode }) => (
  <div className="rounded bg-red-500/10 px-3 py-2 text-xs text-red-400">{children}</div>
)
const Empty = ({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) => (
  <div className="rounded-lg border border-dashed border-border p-5 text-center">
    {icon}
    <p className="mt-2 text-xs text-muted-foreground">{children}</p>
  </div>
)
const Badge = ({ children }: { children: React.ReactNode }) => (
  <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
    {children}
  </span>
)
const DetachButton = ({ onClick }: { onClick: () => void }) => (
  <button
    onClick={onClick}
    className="rounded-lg p-1.5 text-muted-foreground hover:bg-red-500/10 hover:text-red-400"
    title="Detach"
  >
    <Trash2 size={14} />
  </button>
)

// --- Attach sheets ----------------------------------------------------------

function AttachDatasetSheet({
  appId, boundIds, onClose, onAttached,
}: { appId: string; boundIds: Set<string>; onClose: () => void; onAttached: () => void }) {
  const [available, setAvailable] = useState<Dataset[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const [attaching, setAttaching] = useState<string | null>(null)

  useEffect(() => {
    let cancel = false
    apiClient.get<Dataset[]>('/datasets/discoverable')
      .then((d) => { if (!cancel) setAvailable(d) })
      .catch((e: unknown) => { if (!cancel) setError(e instanceof Error ? e.message : String(e)) })
      .finally(() => { if (!cancel) setLoading(false) })
    return () => { cancel = true }
  }, [])

  const filtered = useMemo(() => {
    if (!available) return []
    const q = filter.toLowerCase().trim()
    return available
      .filter((d) => !boundIds.has(d.id))
      .filter((d) => !q || d.name.toLowerCase().includes(q) || (d.description || '').toLowerCase().includes(q))
  }, [available, filter, boundIds])

  const attach = async (id: string) => {
    setAttaching(id)
    try {
      await apiClient.post(`/apps/${appId}/datasets/${id}`)
      onAttached()
      onClose()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setAttaching(null)
    }
  }

  return (
    <Sheet title="Attach a dataset" onClose={onClose}
           filter={filter} setFilter={setFilter}
           note='Only datasets you own or shared with the org are shown. Create new ones in Admin → Datasets.'>
      {loading ? <Spinner /> : error ? <ErrBox>{error}</ErrBox> : filtered.length === 0 ? (
        <Empty icon={<TableIcon size={20} className="mx-auto text-muted-foreground" />}>
          {available && available.length > 0 ? 'No matching datasets.' : 'No datasets available. Create one in Admin → Datasets.'}
        </Empty>
      ) : (
        <ul className="space-y-2">
          {filtered.map((d) => (
            <Row key={d.id} title={d.name} subtitle={d.description}
                 tag={d.kind} tag2={d.visibility}
                 busy={attaching === d.id} onAttach={() => attach(d.id)} />
          ))}
        </ul>
      )}
    </Sheet>
  )
}

function AttachConnectionSheet({
  appId, boundIds, onClose, onAttached,
}: { appId: string; boundIds: Set<string>; onClose: () => void; onAttached: () => void }) {
  const [available, setAvailable] = useState<AppConnection[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const [attaching, setAttaching] = useState<string | null>(null)

  useEffect(() => {
    let cancel = false
    apiClient.get<AppConnection[]>('/connections/callable')
      .then((c) => { if (!cancel) setAvailable(c) })
      .catch((e: unknown) => { if (!cancel) setError(e instanceof Error ? e.message : String(e)) })
      .finally(() => { if (!cancel) setLoading(false) })
    return () => { cancel = true }
  }, [])

  const filtered = useMemo(() => {
    if (!available) return []
    const q = filter.toLowerCase().trim()
    return available
      .filter((c) => !boundIds.has(c.id))
      .filter((c) => !q || c.name.toLowerCase().includes(q) || (c.description || '').toLowerCase().includes(q))
  }, [available, filter, boundIds])

  const attach = async (id: string) => {
    setAttaching(id)
    try {
      await apiClient.post(`/apps/${appId}/connections/${id}`)
      onAttached()
      onClose()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setAttaching(null)
    }
  }

  return (
    <Sheet title="Attach a connection" onClose={onClose}
           filter={filter} setFilter={setFilter}
           note='Only connections an admin has marked "Allow apps to call this connection" appear here — REST APIs and AI providers. Create/enable them in Admin → Connections.'>
      {loading ? <Spinner /> : error ? <ErrBox>{error}</ErrBox> : filtered.length === 0 ? (
        <Empty icon={<Globe size={20} className="mx-auto text-muted-foreground" />}>
          {available && available.length > 0
            ? 'No matching connections.'
            : 'No app-callable connections. In Admin → Connections, add a REST or AI Provider connection and enable "Allow apps to call this connection".'}
        </Empty>
      ) : (
        <ul className="space-y-2">
          {filtered.map((c) => (
            <Row key={c.id} title={c.name} subtitle={c.description || c.base_url}
                 tag={c.kind === 'ai' ? 'ai' : 'rest'} busy={attaching === c.id} onAttach={() => attach(c.id)} />
          ))}
        </ul>
      )}
    </Sheet>
  )
}

function Sheet({
  title, onClose, filter, setFilter, note, children,
}: {
  title: string; onClose: () => void; filter: string; setFilter: (v: string) => void
  note: string; children: React.ReactNode
}) {
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/50 p-4">
      <div className="flex max-h-[80vh] w-full max-w-xl flex-col rounded-lg border border-border bg-card shadow-xl">
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <h2 className="text-sm font-semibold">{title}</h2>
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
          <p className="mt-2 text-xs text-muted-foreground">{note}</p>
        </div>
        <div className="flex-1 overflow-auto px-5 py-3">{children}</div>
      </div>
    </div>
  )
}

function Row({
  title, subtitle, tag, tag2, busy, onAttach,
}: {
  title: string; subtitle?: string; tag?: string; tag2?: string; busy: boolean; onAttach: () => void
}) {
  return (
    <li className="flex items-center justify-between gap-3 rounded-lg border border-border bg-background p-3">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          {tag && <Badge>{tag}</Badge>}
          <h3 className="truncate text-sm font-medium">{title}</h3>
          {tag2 && <span className="rounded bg-accent/40 px-1.5 py-0.5 text-[10px] text-muted-foreground">{tag2}</span>}
        </div>
        {subtitle && <p className="mt-1 truncate text-xs text-muted-foreground">{subtitle}</p>}
      </div>
      <button
        onClick={onAttach}
        disabled={busy}
        className="shrink-0 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
      >
        {busy ? <Loader2 size={12} className="animate-spin" /> : 'Attach'}
      </button>
    </li>
  )
}
