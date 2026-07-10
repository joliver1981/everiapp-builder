import { useEffect, useMemo, useState } from 'react'
import {
  Plus,
  Pencil,
  Trash2,
  Loader2,
  Play,
  Table as TableIcon,
  RefreshCw,
  ChevronRight,
  X,
  Activity,
} from 'lucide-react'
import { PageHeader } from '@/components/layout/PageHeader'
import { apiClient } from '@/api/client'
import { cn } from '@/lib/utils'
import type {
  Connection,
  Dataset,
  DatasetKind,
  DatasetPreviewResult,
  DatasetRecentCallsResult,
  DatasetVisibility,
  SchemaIntrospectionResult,
} from '@/types'

const inputCls =
  'w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring'

const KIND_OPTIONS: { value: DatasetKind; label: string; hint: string }[] = [
  { value: 'query', label: 'SQL Query', hint: 'Raw SQL with :named params' },
  { value: 'table', label: 'Table / View', hint: 'Pick a schema + table' },
  { value: 'api_call', label: 'API Call', hint: 'REST request template' },
]

const VISIBILITY_OPTIONS: { value: DatasetVisibility; label: string; hint: string }[] = [
  { value: 'private', label: 'Private', hint: 'Only you (the owner) can bind it' },
  { value: 'app_scoped', label: 'App-scoped', hint: 'Visible to apps explicitly bound to it' },
  { value: 'org', label: 'Organization', hint: 'Discoverable to any builder; binding still required to execute' },
]

type EditorState = {
  id: string | null
  name: string
  description: string
  connection_id: string
  kind: DatasetKind
  visibility: DatasetVisibility
  row_limit_override: string
  timeout_override: string
  // Query
  query_sql: string
  // Table
  table_schema: string
  table_name: string
  table_columns: string  // comma-separated allowlist
  table_where: string
  // API
  api_method: string
  api_path: string
  api_headers_json: string
  api_query_json: string
  api_body_json: string
  // Params shared by all kinds (preview only)
  params_json: string
}

const EMPTY_EDITOR = (connectionId = ''): EditorState => ({
  id: null,
  name: '',
  description: '',
  connection_id: connectionId,
  kind: 'query',
  visibility: 'private',
  row_limit_override: '',
  timeout_override: '',
  query_sql: 'SELECT 1',
  table_schema: 'main',
  table_name: '',
  table_columns: '',
  table_where: '',
  api_method: 'GET',
  api_path: '/',
  api_headers_json: '{}',
  api_query_json: '{}',
  api_body_json: '',
  params_json: '{}',
})

export function AdminDatasetsPage() {
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [connections, setConnections] = useState<Connection[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [editor, setEditor] = useState<EditorState | null>(null)

  const refresh = async () => {
    setIsLoading(true)
    try {
      const [ds, conns] = await Promise.all([
        apiClient.get<Dataset[]>('/admin/datasets'),
        apiClient.get<Connection[]>('/admin/connections'),
      ])
      setDatasets(ds)
      setConnections(conns)
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  // AI-provider connections can't back datasets — keep them out of this page's picker.
  const datasetConnections = useMemo(
    () => connections.filter((c) => c.kind !== 'ai'),
    [connections],
  )

  const openCreate = () => {
    setEditor(EMPTY_EDITOR(datasetConnections[0]?.id || ''))
  }

  const openEdit = (d: Dataset) => {
    const def = d.definition as Record<string, unknown>
    setEditor({
      id: d.id,
      name: d.name,
      description: d.description || '',
      connection_id: d.connection_id,
      kind: d.kind,
      visibility: d.visibility,
      row_limit_override: d.row_limit_override != null ? String(d.row_limit_override) : '',
      timeout_override: d.timeout_override != null ? String(d.timeout_override) : '',
      query_sql: (def.sql as string) || 'SELECT 1',
      table_schema: (def.schema as string) || 'main',
      table_name: (def.table_name as string) || '',
      table_columns: Array.isArray(def.column_allowlist) ? (def.column_allowlist as string[]).join(', ') : '',
      table_where: (def.where_template as string) || '',
      api_method: (def.method as string) || 'GET',
      api_path: (def.path as string) || '/',
      api_headers_json: JSON.stringify(def.headers || {}, null, 2),
      api_query_json: JSON.stringify(def.query_params || {}, null, 2),
      api_body_json: def.body_template ? JSON.stringify(def.body_template, null, 2) : '',
      params_json: '{}',
    })
  }

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`Delete dataset '${name}'? Apps bound to it will start failing.`)) return
    await apiClient.delete(`/admin/datasets/${id}`)
    refresh()
  }

  const connectionsById = useMemo(() => {
    const m = new Map<string, Connection>()
    connections.forEach((c) => m.set(c.id, c))
    return m
  }, [connections])

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Datasets"
        description="Named, parameterized queries and API calls built on top of Connections. Apps consume these via the runtime proxy."
        actions={
          <button
            onClick={openCreate}
            disabled={datasetConnections.length === 0}
            className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            title={datasetConnections.length === 0 ? 'Create a SQL or REST Connection first' : 'Create a new dataset'}
          >
            <Plus size={16} /> New Dataset
          </button>
        }
      />

      <div className="flex-1 overflow-auto px-8 py-6">
        {isLoading ? (
          <div className="flex justify-center py-12">
            <Loader2 size={28} className="animate-spin text-muted-foreground" />
          </div>
        ) : connections.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border bg-card p-12 text-center">
            <TableIcon size={32} className="mx-auto text-muted-foreground" />
            <h3 className="mt-4 text-lg font-medium">No connections yet</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              Datasets build on Connections. Create one in the Connections page first.
            </p>
          </div>
        ) : datasets.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border bg-card p-12 text-center">
            <TableIcon size={32} className="mx-auto text-muted-foreground" />
            <h3 className="mt-4 text-lg font-medium">No datasets yet</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              Click "New Dataset" to create one. It can be a raw SQL query, a table/view picker, or an API call.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {datasets.map((d) => (
              <DatasetRow
                key={d.id}
                dataset={d}
                connectionName={connectionsById.get(d.connection_id)?.name || '(missing)'}
                onEdit={() => openEdit(d)}
                onDelete={() => handleDelete(d.id, d.name)}
              />
            ))}
          </div>
        )}
      </div>

      {editor && (
        <DatasetEditor
          state={editor}
          setState={setEditor}
          connections={datasetConnections}
          onClose={() => setEditor(null)}
          onSaved={() => {
            setEditor(null)
            refresh()
          }}
        />
      )}
    </div>
  )
}

function DatasetRow({
  dataset,
  connectionName,
  onEdit,
  onDelete,
}: {
  dataset: Dataset
  connectionName: string
  onEdit: () => void
  onDelete: () => void
}) {
  const def = dataset.definition as Record<string, unknown>
  const summary =
    dataset.kind === 'query'
      ? truncate((def.sql as string) || '', 80)
      : dataset.kind === 'table'
        ? `${(def.schema as string) || ''}${def.schema ? '.' : ''}${(def.table_name as string) || '?'}`
        : `${(def.method as string) || 'GET'} ${(def.path as string) || '/'}`

  const [callsOpen, setCallsOpen] = useState(false)

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between gap-4 p-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="rounded bg-secondary px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {dataset.kind}
            </span>
            <h3 className="truncate font-medium">{dataset.name}</h3>
            <span className="rounded bg-accent/40 px-2 py-0.5 text-xs text-muted-foreground">
              {dataset.visibility}
            </span>
            <span className="text-xs text-muted-foreground">on {connectionName}</span>
          </div>
          {dataset.description && (
            <p className="mt-1 text-sm text-muted-foreground">{dataset.description}</p>
          )}
          <p className="mt-1 truncate font-mono text-xs text-muted-foreground">{summary}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            onClick={() => setCallsOpen(!callsOpen)}
            title="Recent calls"
            className={cn(
              'rounded-lg p-2 hover:bg-accent hover:text-foreground',
              callsOpen ? 'text-primary' : 'text-muted-foreground',
            )}
          >
            <Activity size={16} />
          </button>
          <button
            onClick={onEdit}
            className="rounded-lg p-2 text-muted-foreground hover:bg-accent hover:text-foreground"
          >
            <Pencil size={16} />
          </button>
          <button
            onClick={onDelete}
            className="rounded-lg p-2 text-muted-foreground hover:bg-red-500/10 hover:text-red-400"
          >
            <Trash2 size={16} />
          </button>
        </div>
      </div>
      {callsOpen && (
        <div className="border-t border-border bg-background/40 px-4 py-3">
          <RecentCallsPanel datasetId={dataset.id} />
        </div>
      )}
    </div>
  )
}

function RecentCallsPanel({ datasetId }: { datasetId: string }) {
  const [data, setData] = useState<DatasetRecentCallsResult | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await apiClient.get<DatasetRecentCallsResult>(
        `/admin/datasets/${datasetId}/recent-calls?limit=20`,
      )
      setData(r)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasetId])

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Recent calls</h4>
        <button
          onClick={refresh}
          disabled={loading}
          className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
          title="Refresh"
        >
          {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
        </button>
      </div>
      {error ? (
        <p className="text-xs text-red-400">{error}</p>
      ) : !data ? (
        <p className="text-xs text-muted-foreground">Loading…</p>
      ) : data.calls.length === 0 ? (
        <p className="text-xs text-muted-foreground">No calls yet. Hits from deployed apps will show up here.</p>
      ) : (
        <ul className="space-y-1">
          {data.calls.map((c, i) => (
            <li key={i} className="rounded border border-border/40 bg-background px-2 py-1 text-xs">
              <div className="flex items-center justify-between gap-2">
                <span
                  className={cn(
                    'rounded px-1.5 py-0.5 text-[10px] font-medium',
                    c.action === 'dataset.execute'
                      ? 'bg-green-500/10 text-green-400'
                      : 'bg-red-500/10 text-red-400',
                  )}
                >
                  {c.action === 'dataset.execute' ? 'OK' : 'ERROR'}
                </span>
                <span className="font-mono text-[10px] text-muted-foreground">{c.created_at}</span>
              </div>
              <p className="mt-1 truncate font-mono text-[11px] text-muted-foreground">{c.details}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// --- Editor ---------------------------------------------------------------

function DatasetEditor({
  state,
  setState,
  connections,
  onClose,
  onSaved,
}: {
  state: EditorState
  setState: (s: EditorState) => void
  connections: Connection[]
  onClose: () => void
  onSaved: () => void
}) {
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [preview, setPreview] = useState<DatasetPreviewResult | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [previewWidth, setPreviewWidth] = useState(420)

  // Drag the divider between the form and the preview to resize the preview pane.
  const startResize = (e: React.MouseEvent) => {
    e.preventDefault()
    const startX = e.clientX
    const startWidth = previewWidth
    const onMove = (ev: MouseEvent) => {
      // Pane is anchored right, so dragging left (smaller clientX) widens it.
      const next = startWidth + (startX - ev.clientX)
      const max = Math.max(320, window.innerWidth - 600)
      setPreviewWidth(Math.min(Math.max(next, 280), max))
    }
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'col-resize'
  }

  const set = <K extends keyof EditorState>(k: K, v: EditorState[K]) =>
    setState({ ...state, [k]: v })

  const conn = connections.find((c) => c.id === state.connection_id) || null

  const buildDefinition = (): Record<string, unknown> => {
    if (state.kind === 'query') return { sql: state.query_sql }
    if (state.kind === 'table') {
      const def: Record<string, unknown> = {
        schema: state.table_schema || undefined,
        table_name: state.table_name,
      }
      const allow = state.table_columns
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
      if (allow.length) def.column_allowlist = allow
      if (state.table_where) def.where_template = state.table_where
      return def
    }
    // api_call
    let headers: unknown = {}
    let queryParams: unknown = {}
    let body: unknown = undefined
    try { headers = state.api_headers_json ? JSON.parse(state.api_headers_json) : {} } catch { /* ignore */ }
    try { queryParams = state.api_query_json ? JSON.parse(state.api_query_json) : {} } catch { /* ignore */ }
    try { body = state.api_body_json ? JSON.parse(state.api_body_json) : undefined } catch { /* ignore */ }
    return {
      method: state.api_method,
      path: state.api_path,
      headers,
      query_params: queryParams,
      body_template: body,
    }
  }

  const handleSave = async () => {
    setSaving(true)
    setError(null)
    try {
      const payload = {
        name: state.name,
        description: state.description,
        connection_id: state.connection_id,
        kind: state.kind,
        definition: buildDefinition(),
        visibility: state.visibility,
        row_limit_override: state.row_limit_override ? Number(state.row_limit_override) : null,
        timeout_override: state.timeout_override ? Number(state.timeout_override) : null,
      }
      if (state.id) {
        await apiClient.put<Dataset>(`/admin/datasets/${state.id}`, payload)
      } else {
        await apiClient.post<Dataset>('/admin/datasets', payload)
      }
      onSaved()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(`Save failed: ${msg}`)
    } finally {
      setSaving(false)
    }
  }

  const handlePreview = async () => {
    setPreviewing(true)
    setError(null)
    setPreview(null)
    try {
      let params: Record<string, unknown> = {}
      try { params = state.params_json ? JSON.parse(state.params_json) : {} } catch {
        throw new Error('Params JSON is invalid')
      }
      const result = await apiClient.post<DatasetPreviewResult>('/admin/datasets/preview', {
        connection_id: state.connection_id,
        kind: state.kind,
        definition: buildDefinition(),
        params,
      })
      setPreview(result)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(`Preview failed: ${msg}`)
    } finally {
      setPreviewing(false)
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-stretch justify-stretch bg-black/50 p-4">
      <div className="flex w-full flex-col rounded-lg border border-border bg-card shadow-xl">
        <div className="flex items-center justify-between border-b border-border px-6 py-4">
          <h2 className="text-lg font-semibold">{state.id ? 'Edit dataset' : 'New dataset'}</h2>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
            <X size={20} />
          </button>
        </div>

        <div className="flex flex-1 min-h-0 gap-0">
          {/* Left: Schema browser (SQL connections only) */}
          {conn && conn.kind === 'sql' && (
            <SchemaBrowser
              connection={conn}
              onPick={(table, schema) => {
                set('kind', 'table')
                set('table_schema', schema)
                set('table_name', table)
              }}
            />
          )}

          {/* Center: form */}
          <div className="flex flex-1 flex-col min-w-0 overflow-auto px-6 py-4 space-y-3">
            <Field label="Name">
              <input value={state.name} onChange={(e) => set('name', e.target.value)} className={inputCls} placeholder="recent_orders" />
            </Field>
            <Field label="Description" optional>
              <input value={state.description} onChange={(e) => set('description', e.target.value)} className={inputCls} />
            </Field>

            <div className="grid grid-cols-2 gap-3">
              <Field label="Connection">
                <select
                  value={state.connection_id}
                  onChange={(e) => set('connection_id', e.target.value)}
                  className={inputCls}
                >
                  {connections.map((c) => (
                    <option key={c.id} value={c.id}>{c.name} ({c.kind})</option>
                  ))}
                </select>
              </Field>
              <Field label="Visibility">
                <select
                  value={state.visibility}
                  onChange={(e) => set('visibility', e.target.value as DatasetVisibility)}
                  className={inputCls}
                >
                  {VISIBILITY_OPTIONS.map((v) => (
                    <option key={v.value} value={v.value}>{v.label}</option>
                  ))}
                </select>
              </Field>
            </div>

            {/* Kind tabs */}
            <div className="flex gap-1 rounded-lg bg-secondary p-1">
              {KIND_OPTIONS.map((k) => (
                <button
                  key={k.value}
                  onClick={() => set('kind', k.value)}
                  className={cn(
                    'flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
                    state.kind === k.value ? 'bg-background shadow' : 'text-muted-foreground hover:text-foreground',
                  )}
                  title={k.hint}
                >
                  {k.label}
                </button>
              ))}
            </div>

            {state.kind === 'query' && (
              <Field
                label="SQL"
                hint="Use :name for params. The platform will inject :current_user automatically at runtime."
              >
                <textarea
                  value={state.query_sql}
                  onChange={(e) => set('query_sql', e.target.value)}
                  className={cn(inputCls, 'min-h-[140px] font-mono')}
                />
              </Field>
            )}

            {state.kind === 'table' && (
              <>
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Schema">
                    <input value={state.table_schema} onChange={(e) => set('table_schema', e.target.value)} className={inputCls} placeholder="main" />
                  </Field>
                  <Field label="Table">
                    <input value={state.table_name} onChange={(e) => set('table_name', e.target.value)} className={inputCls} placeholder="orders" />
                  </Field>
                </div>
                <Field label="Column allowlist (comma-separated)" optional>
                  <input value={state.table_columns} onChange={(e) => set('table_columns', e.target.value)} className={inputCls} placeholder="id, total, customer_id" />
                </Field>
                <Field label="WHERE clause template" optional hint="e.g. customer_id = :customer_id">
                  <input value={state.table_where} onChange={(e) => set('table_where', e.target.value)} className={inputCls} />
                </Field>
              </>
            )}

            {state.kind === 'api_call' && (
              <>
                <div className="grid grid-cols-[120px_1fr] gap-3">
                  <Field label="Method">
                    <select value={state.api_method} onChange={(e) => set('api_method', e.target.value)} className={inputCls}>
                      {['GET', 'POST', 'PUT', 'DELETE', 'PATCH'].map((m) => (
                        <option key={m} value={m}>{m}</option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Path" hint="Use {{param}} for substitution. Joined onto the connection's base_url.">
                    <input value={state.api_path} onChange={(e) => set('api_path', e.target.value)} className={inputCls} placeholder="/customers/{{customer_id}}" />
                  </Field>
                </div>
                <Field label="Headers (JSON)" optional>
                  <textarea value={state.api_headers_json} onChange={(e) => set('api_headers_json', e.target.value)} className={cn(inputCls, 'min-h-[60px] font-mono')} />
                </Field>
                <Field label="Query params (JSON)" optional>
                  <textarea value={state.api_query_json} onChange={(e) => set('api_query_json', e.target.value)} className={cn(inputCls, 'min-h-[60px] font-mono')} />
                </Field>
                <Field label="Body template (JSON)" optional>
                  <textarea value={state.api_body_json} onChange={(e) => set('api_body_json', e.target.value)} className={cn(inputCls, 'min-h-[80px] font-mono')} placeholder="(leave blank for none)" />
                </Field>
              </>
            )}

            <div className="grid grid-cols-2 gap-3">
              <Field label="Row limit override" optional>
                <input value={state.row_limit_override} onChange={(e) => set('row_limit_override', e.target.value)} className={inputCls} placeholder="(use connection default)" />
              </Field>
              <Field label="Timeout (s) override" optional>
                <input value={state.timeout_override} onChange={(e) => set('timeout_override', e.target.value)} className={inputCls} placeholder="(use connection default)" />
              </Field>
            </div>
          </div>

          {/* Drag handle to resize the preview pane */}
          <div
            onMouseDown={startResize}
            role="separator"
            aria-orientation="vertical"
            title="Drag to resize the preview"
            className="w-1.5 shrink-0 cursor-col-resize bg-border transition-colors hover:bg-primary/60"
          />

          {/* Right: preview pane */}
          <div
            style={{ width: previewWidth }}
            className="flex shrink-0 flex-col bg-background/40"
          >
            <div className="border-b border-border px-4 py-3">
              <h3 className="text-sm font-medium">Preview</h3>
              <p className="mt-0.5 text-xs text-muted-foreground">Caps at 100 rows. Runs against the real connection.</p>
              <Field label="Params (JSON)" optional>
                <textarea
                  value={state.params_json}
                  onChange={(e) => set('params_json', e.target.value)}
                  className={cn(inputCls, 'min-h-[60px] font-mono text-xs')}
                />
              </Field>
              <button
                onClick={handlePreview}
                disabled={previewing}
                className="mt-2 flex w-full items-center justify-center gap-2 rounded-lg bg-secondary px-3 py-2 text-sm font-medium hover:bg-secondary/80 disabled:opacity-50"
              >
                {previewing ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
                Run preview
              </button>
            </div>
            <div className="flex-1 overflow-auto p-3">
              {preview ? (
                <PreviewTable result={preview} />
              ) : (
                <p className="text-xs text-muted-foreground">No preview yet. Click "Run preview" to execute.</p>
              )}
            </div>
          </div>
        </div>

        {error && (
          <div className="border-t border-border bg-red-500/10 px-6 py-2 text-sm text-red-400">
            {error}
          </div>
        )}

        <div className="flex items-center justify-end gap-2 border-t border-border px-6 py-4">
          <button onClick={onClose} className="rounded-lg px-4 py-2 text-sm text-muted-foreground hover:text-foreground">
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving || !state.name || !state.connection_id}
            className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {saving ? <Loader2 size={16} className="animate-spin" /> : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}

// --- Schema browser (SQL only) -------------------------------------------

function SchemaBrowser({
  connection,
  onPick,
}: {
  connection: Connection
  onPick: (table: string, schema: string) => void
}) {
  const [schemas, setSchemas] = useState<string[] | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [tables, setTables] = useState<Record<string, string[]>>({})
  const [loading, setLoading] = useState(false)

  const refreshSchemas = async () => {
    setLoading(true)
    try {
      const r = await apiClient.get<SchemaIntrospectionResult>(
        `/admin/connections/${connection.id}/schema`,
      )
      setSchemas(r.schemas)
      // Auto-expand if only one schema (sqlite case)
      if (r.schemas.length === 1) {
        setExpanded(r.schemas[0])
        await loadTables(r.schemas[0])
      }
    } catch { /* swallow — show empty state */ }
    finally { setLoading(false) }
  }

  const loadTables = async (schema: string) => {
    try {
      const r = await apiClient.get<SchemaIntrospectionResult>(
        `/admin/connections/${connection.id}/schema?schema=${encodeURIComponent(schema)}`,
      )
      setTables((prev) => ({ ...prev, [schema]: r.tables }))
    } catch { /* ignore */ }
  }

  useEffect(() => {
    setSchemas(null)
    setExpanded(null)
    setTables({})
    refreshSchemas()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connection.id])

  return (
    <div className="flex w-[240px] shrink-0 flex-col border-r border-border bg-background/40">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <h3 className="text-sm font-medium">Schema</h3>
        <button
          onClick={refreshSchemas}
          disabled={loading}
          title="Refresh schema"
          className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
        >
          {loading ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
        </button>
      </div>
      <div className="flex-1 overflow-auto p-2 text-xs">
        {!schemas ? (
          <p className="text-muted-foreground">Loading…</p>
        ) : schemas.length === 0 ? (
          <p className="text-muted-foreground">No schemas found.</p>
        ) : (
          <ul className="space-y-1">
            {schemas.map((s) => (
              <li key={s}>
                <button
                  onClick={() => {
                    if (expanded === s) { setExpanded(null) } else {
                      setExpanded(s)
                      if (!tables[s]) loadTables(s)
                    }
                  }}
                  className="flex w-full items-center gap-1 rounded px-1.5 py-1 text-left font-medium hover:bg-accent"
                >
                  <ChevronRight size={12} className={cn('transition-transform', expanded === s && 'rotate-90')} />
                  {s}
                </button>
                {expanded === s && (
                  <ul className="ml-4 mt-1 space-y-0.5">
                    {(tables[s] || []).map((t) => (
                      <li key={t}>
                        <button
                          onClick={() => onPick(t, s)}
                          className="block w-full truncate rounded px-1.5 py-0.5 text-left font-mono text-muted-foreground hover:bg-accent hover:text-foreground"
                          title={`Pick ${s}.${t}`}
                        >
                          {t}
                        </button>
                      </li>
                    ))}
                    {tables[s]?.length === 0 && (
                      <li className="px-1.5 py-0.5 text-muted-foreground">(empty)</li>
                    )}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

// --- Preview table --------------------------------------------------------

function PreviewTable({ result }: { result: DatasetPreviewResult }) {
  if (result.rows.length === 0) {
    return (
      <div className="text-xs text-muted-foreground">
        0 rows in {result.duration_ms}ms.
      </div>
    )
  }
  return (
    <div className="space-y-2">
      <div className="text-xs text-muted-foreground">
        {result.row_count} rows {result.truncated && '(truncated)'} · {result.duration_ms}ms
      </div>
      <div className="overflow-x-auto rounded border border-border">
        <table className="min-w-full text-xs">
          <thead className="bg-secondary">
            <tr>
              {result.columns.map((c) => (
                <th key={c.name} className="whitespace-nowrap border-b border-border px-2 py-1 text-left font-medium">{c.name}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.map((r, i) => (
              <tr key={i} className="border-b border-border/40 last:border-0">
                {result.columns.map((c) => {
                  const cell = formatCell(r[c.name])
                  return (
                    <td key={c.name} title={cell} className="max-w-[280px] truncate px-2 py-1 font-mono text-muted-foreground">
                      {cell}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function formatCell(v: unknown): string {
  if (v === null || v === undefined) return '∅'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

// --- helpers --------------------------------------------------------------

function Field({
  label,
  children,
  optional,
  hint,
}: {
  label: string
  children: React.ReactNode
  optional?: boolean
  hint?: string
}) {
  return (
    <div>
      <label className="block text-sm font-medium">
        {label}
        {optional && <span className="ml-1 text-xs text-muted-foreground">(optional)</span>}
      </label>
      <div className="mt-1">{children}</div>
      {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
    </div>
  )
}

function truncate(s: string, n: number) {
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}
