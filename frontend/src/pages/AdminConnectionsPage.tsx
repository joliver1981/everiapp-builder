import { useEffect, useState } from 'react'
import {
  Plus,
  Pencil,
  Trash2,
  Loader2,
  CheckCircle,
  XCircle,
  Play,
  Database,
  RefreshCw,
  X,
} from 'lucide-react'
import { PageHeader } from '@/components/layout/PageHeader'
import { apiClient } from '@/api/client'
import { cn } from '@/lib/utils'
import type { Connection, ConnectionKind, ConnectionTestResult, Secret } from '@/types'

const SQL_DIALECTS = [
  { value: 'sqlite', label: 'SQLite' },
  { value: 'postgres', label: 'PostgreSQL' },
  { value: 'mysql', label: 'MySQL' },
  { value: 'mssql', label: 'SQL Server' },
  { value: 'oracle', label: 'Oracle' },
]

const inputCls =
  'w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring'

const REST_AUTH_TYPES = [
  { value: 'none', label: 'None' },
  { value: 'bearer', label: 'Bearer token' },
  { value: 'basic', label: 'Basic (user:pass)' },
  { value: 'api_key_header', label: 'API key in header' },
  { value: 'api_key_query', label: 'API key in query param' },
]

const KIND_LABELS: Record<ConnectionKind, string> = {
  sql: 'SQL',
  rest: 'REST',
  ai: 'AI Provider',
}

// Display fallback only — the authoritative preset list (base URLs, auth,
// suggested models) comes from GET /admin/connections/ai-providers.
const AI_PROVIDER_LABELS: Record<string, string> = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  openrouter: 'OpenRouter',
  azure_openai: 'Azure OpenAI',
  custom: 'Custom',
}

type AIProviderPreset = {
  provider: string
  label: string
  base_url: string
  auth_type: string
  auth_param: string | null
  default_headers: Record<string, string>
  default_query: Record<string, string>
  models_path: string
  chat_path: string
  api_format: string
  suggested_models: string[]
  hint: string
}

type FormState = {
  name: string
  description: string
  kind: ConnectionKind
  credential_secret_ref: string
  default_row_limit: number
  default_timeout_seconds: number
  read_only: boolean
  app_callable: boolean
  // SQL
  sql_dialect: string
  sql_host: string
  sql_port: string
  sql_database: string
  sql_username: string
  // REST (base_url/auth shared with AI connections)
  rest_base_url: string
  rest_auth_type: string
  rest_auth_param: string
  // AI
  ai_provider: string
  ai_models: string[]
  ai_default_model: string
  ai_default_headers: Record<string, string>
  ai_default_query: Record<string, string>
  // Passthrough-only (never rendered as inputs) — carried so an edit+save
  // round-trip doesn't drop API-set values like a custom chat_path.
  ai_chat_path: string
  ai_models_path: string
  ai_api_format: string
}

const EMPTY_FORM: FormState = {
  name: '',
  description: '',
  kind: 'sql',
  credential_secret_ref: '',
  default_row_limit: 500000,
  default_timeout_seconds: 30,
  read_only: true,
  app_callable: false,
  sql_dialect: 'sqlite',
  sql_host: '',
  sql_port: '',
  sql_database: '',
  sql_username: '',
  rest_base_url: '',
  rest_auth_type: 'none',
  rest_auth_param: '',
  ai_provider: '',
  ai_models: [],
  ai_default_model: '',
  ai_default_headers: {},
  ai_default_query: {},
  ai_chat_path: '',
  ai_models_path: '',
  ai_api_format: '',
}

// GET /admin/connections scrubs secret-looking config values to this literal —
// they must never be written back on save.
const REDACTED = '***REDACTED***'
const stripRedacted = (obj: Record<string, string> | undefined): Record<string, string> =>
  Object.fromEntries(Object.entries(obj || {}).filter(([, v]) => v !== REDACTED))

// apiClient errors often carry the raw JSON body as the message — surface the
// backend's human-readable {detail} when present.
const errDetail = (e: unknown): string => {
  const msg = e instanceof Error ? e.message : String(e)
  try {
    const parsed = JSON.parse(msg) as { detail?: unknown }
    if (parsed && typeof parsed.detail === 'string') return parsed.detail
  } catch {
    // not JSON — fall through to the raw message
  }
  return msg
}

// Pure so the modal can reuse it for the fetch-models preview call. Any config
// key not carried through FormState is dropped on edit+save — keep in sync.
const buildConfig = (form: FormState): Record<string, unknown> => {
  if (form.kind === 'sql') {
    const cfg: Record<string, unknown> = { dialect: form.sql_dialect }
    if (form.sql_host) cfg.host = form.sql_host
    if (form.sql_port) cfg.port = Number(form.sql_port)
    if (form.sql_database) cfg.database = form.sql_database
    if (form.sql_username) cfg.username = form.sql_username
    return cfg
  }
  if (form.kind === 'ai') {
    const cfg: Record<string, unknown> = {
      provider: form.ai_provider,
      base_url: form.rest_base_url,
      auth_type: form.rest_auth_type,
      default_headers: stripRedacted(form.ai_default_headers),
      default_query: stripRedacted(form.ai_default_query),
      models: form.ai_models,
    }
    if (
      (form.rest_auth_type === 'api_key_header' || form.rest_auth_type === 'api_key_query') &&
      form.rest_auth_param
    ) {
      cfg.auth_param = form.rest_auth_param
    }
    if (form.ai_default_model) cfg.default_model = form.ai_default_model
    // Passthrough of API-set keys (presets fill these too) — omitted when empty
    // so the backend falls back to the provider preset's values.
    if (form.ai_chat_path) cfg.chat_path = form.ai_chat_path
    if (form.ai_models_path) cfg.models_path = form.ai_models_path
    if (form.ai_api_format) cfg.api_format = form.ai_api_format
    return cfg
  }
  const cfg: Record<string, unknown> = {
    base_url: form.rest_base_url,
    auth_type: form.rest_auth_type,
  }
  if (form.rest_auth_param) cfg.auth_param = form.rest_auth_param
  const restHeaders = stripRedacted(form.ai_default_headers)
  const restQuery = stripRedacted(form.ai_default_query)
  if (Object.keys(restHeaders).length > 0) cfg.default_headers = restHeaders
  if (Object.keys(restQuery).length > 0) cfg.default_query = restQuery
  return cfg
}

export function AdminConnectionsPage() {
  const [connections, setConnections] = useState<Connection[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [testingId, setTestingId] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{ id: string; result: ConnectionTestResult } | null>(null)
  const [isSaving, setIsSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  // AI provider presets for the "AI Provider" kind — fetched once per page load.
  const [aiPresets, setAiPresets] = useState<AIProviderPreset[] | null>(null)
  const [aiPresetsError, setAiPresetsError] = useState<string | null>(null)
  // Secrets for the credential dropdown. null = not loaded yet; on fetch failure
  // the modal falls back to a free-text input, so the fetch never blocks it.
  const [secrets, setSecrets] = useState<Secret[] | null>(null)
  const [secretsError, setSecretsError] = useState(false)

  const fetchConnections = async () => {
    setIsLoading(true)
    try {
      const data = await apiClient.get<Connection[]>('/admin/connections')
      setConnections(data)
    } finally {
      setIsLoading(false)
    }
  }

  const fetchSecrets = () => {
    apiClient
      .get<Secret[]>('/secrets')
      .then((s) => {
        setSecrets(s)
        setSecretsError(false)
      })
      .catch(() => setSecretsError(true))
  }

  useEffect(() => {
    fetchConnections()
    fetchSecrets()
    apiClient
      .get<{ providers: AIProviderPreset[] }>('/admin/connections/ai-providers')
      .then((d) => setAiPresets(d.providers))
      .catch((e: unknown) => setAiPresetsError(e instanceof Error ? e.message : String(e)))
  }, [])

  const openCreate = () => {
    setEditingId(null)
    setForm(EMPTY_FORM)
    setFormError(null)
    fetchSecrets() // pick up secrets created in another tab mid-session
    setShowForm(true)
  }

  const openEdit = (c: Connection) => {
    setEditingId(c.id)
    setFormError(null)
    const cfg = (c.config || {}) as Record<string, unknown>
    setForm({
      name: c.name,
      description: c.description || '',
      kind: c.kind,
      credential_secret_ref: c.credential_secret_ref || '',
      default_row_limit: c.default_row_limit,
      default_timeout_seconds: c.default_timeout_seconds,
      read_only: c.read_only,
      app_callable: c.app_callable,
      sql_dialect: (cfg.dialect as string) || 'sqlite',
      sql_host: (cfg.host as string) || '',
      sql_port: cfg.port ? String(cfg.port) : '',
      sql_database: (cfg.database as string) || '',
      sql_username: (cfg.username as string) || '',
      rest_base_url: (cfg.base_url as string) || '',
      rest_auth_type: (cfg.auth_type as string) || 'none',
      rest_auth_param: (cfg.auth_param as string) || '',
      ai_provider: (cfg.provider as string) || '',
      ai_models: Array.isArray(cfg.models) ? (cfg.models as string[]) : [],
      ai_default_model: (cfg.default_model as string) || '',
      ai_default_headers: (cfg.default_headers as Record<string, string>) || {},
      ai_default_query: (cfg.default_query as Record<string, string>) || {},
      ai_chat_path: (cfg.chat_path as string) || '',
      ai_models_path: (cfg.models_path as string) || '',
      ai_api_format: (cfg.api_format as string) || '',
    })
    fetchSecrets() // pick up secrets created in another tab mid-session
    setShowForm(true)
  }

  const closeForm = () => {
    setShowForm(false)
    setEditingId(null)
    setForm(EMPTY_FORM)
    setFormError(null)
  }

  const handleSubmit = async (andTest = false) => {
    setIsSaving(true)
    setFormError(null)
    try {
      const payload = {
        name: form.name,
        description: form.description,
        kind: form.kind,
        config: buildConfig(form),
        credential_secret_ref: form.credential_secret_ref || null,
        default_row_limit: form.default_row_limit,
        default_timeout_seconds: form.default_timeout_seconds,
        read_only: form.read_only,
        app_callable: form.kind === 'rest' || form.kind === 'ai' ? form.app_callable : false,
      }
      let id: string
      if (editingId) {
        const updated = await apiClient.put<Connection>(
          `/admin/connections/${editingId}`,
          payload,
        )
        id = updated.id
      } else {
        const created = await apiClient.post<Connection>('/admin/connections', payload)
        id = created.id
      }
      closeForm()
      await fetchConnections()
      if (andTest) handleTest(id)
    } catch (e: unknown) {
      setFormError(`Save failed: ${errDetail(e)}`)
    } finally {
      setIsSaving(false)
    }
  }

  const handleTest = async (id: string) => {
    setTestingId(id)
    setTestResult(null)
    try {
      const result = await apiClient.post<ConnectionTestResult>(
        `/admin/connections/${id}/test`,
      )
      setTestResult({ id, result })
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      setTestResult({ id, result: { success: false, message: msg, response_time_ms: null } })
    } finally {
      setTestingId(null)
    }
  }

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`Delete connection '${name}'? Datasets that depend on it will fail at runtime.`)) return
    await apiClient.delete(`/admin/connections/${id}`)
    fetchConnections()
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Connections"
        description="Reusable, credentialed pointers to external SQL databases, REST APIs, and AI providers. Datasets and apps build on top of these."
        actions={
          <button
            onClick={openCreate}
            className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          >
            <Plus size={16} /> New Connection
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
            <Database size={32} className="mx-auto text-muted-foreground" />
            <h3 className="mt-4 text-lg font-medium">No connections yet</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              Create one to start building datasets you can reuse across apps.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {connections.map((c) => (
              <ConnectionRow
                key={c.id}
                connection={c}
                onEdit={() => openEdit(c)}
                onDelete={() => handleDelete(c.id, c.name)}
                onTest={() => handleTest(c.id)}
                testing={testingId === c.id}
                testResult={testResult?.id === c.id ? testResult.result : null}
              />
            ))}
          </div>
        )}
      </div>

      {showForm && (
        <ConnectionFormModal
          form={form}
          setForm={setForm}
          editingId={editingId}
          formError={formError}
          isSaving={isSaving}
          aiPresets={aiPresets}
          aiPresetsError={aiPresetsError}
          secrets={secrets}
          secretsError={secretsError}
          onClose={closeForm}
          onSubmit={handleSubmit}
        />
      )}
    </div>
  )
}

// --- Row ------------------------------------------------------------------

function ConnectionRow({
  connection,
  onEdit,
  onDelete,
  onTest,
  testing,
  testResult,
}: {
  connection: Connection
  onEdit: () => void
  onDelete: () => void
  onTest: () => void
  testing: boolean
  testResult: ConnectionTestResult | null
}) {
  const cfg = connection.config as Record<string, unknown>
  const aiModels = Array.isArray(cfg.models) ? (cfg.models as string[]) : []
  const summary =
    connection.kind === 'sql'
      ? `${(cfg.dialect as string) || '?'}${cfg.host ? ` @ ${cfg.host}` : ''}${cfg.database ? `/${cfg.database}` : ''}`
      : connection.kind === 'ai' && aiModels.length > 0
        ? `${AI_PROVIDER_LABELS[cfg.provider as string] || (cfg.provider as string) || 'AI'} · ${aiModels.length} model${aiModels.length === 1 ? '' : 's'}`
        : (cfg.base_url as string) || '(no base URL)'

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="rounded bg-secondary px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {connection.kind}
            </span>
            <h3 className="truncate font-medium">{connection.name}</h3>
            {connection.read_only && (
              <span className="rounded bg-blue-500/10 px-2 py-0.5 text-xs text-blue-400">read-only</span>
            )}
            {connection.app_callable && (
              <span className="rounded bg-primary/10 px-2 py-0.5 text-xs text-primary">app-callable</span>
            )}
          </div>
          {connection.description && (
            <p className="mt-1 text-sm text-muted-foreground">{connection.description}</p>
          )}
          <p className="mt-1 truncate font-mono text-xs text-muted-foreground">{summary}</p>
          {testResult && (
            <div
              className={cn(
                'mt-2 flex items-center gap-2 rounded-md px-2 py-1 text-xs',
                testResult.success
                  ? 'bg-green-500/10 text-green-400'
                  : 'bg-red-500/10 text-red-400',
              )}
            >
              {testResult.success ? <CheckCircle size={14} /> : <XCircle size={14} />}
              <span className="truncate">
                {testResult.message}
                {testResult.response_time_ms != null && ` (${testResult.response_time_ms}ms)`}
              </span>
            </div>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            onClick={onTest}
            disabled={testing}
            title="Test connection"
            className="rounded-lg p-2 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
          >
            {testing ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
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
    </div>
  )
}

// --- Form modal -----------------------------------------------------------

function ConnectionFormModal({
  form,
  setForm,
  editingId,
  formError,
  isSaving,
  aiPresets,
  aiPresetsError,
  secrets,
  secretsError,
  onClose,
  onSubmit,
}: {
  form: FormState
  setForm: React.Dispatch<React.SetStateAction<FormState>>
  editingId: string | null
  formError: string | null
  isSaving: boolean
  aiPresets: AIProviderPreset[] | null
  aiPresetsError: string | null
  secrets: Secret[] | null
  secretsError: boolean
  onClose: () => void
  onSubmit: (andTest?: boolean) => void
}) {
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) =>
    setForm({ ...form, [k]: v })

  const [modelInput, setModelInput] = useState('')
  const [fetchingModels, setFetchingModels] = useState(false)
  const [fetchModelsError, setFetchModelsError] = useState<string | null>(null)

  const selectedPreset = aiPresets?.find((p) => p.provider === form.ai_provider) || null

  // ONE setForm call — sequential set() calls would clobber each other.
  const applyPreset = (p: AIProviderPreset) => {
    // Re-clicking the already-selected card must not reset a customized base_url/auth.
    if (p.provider === form.ai_provider) return
    const seedModels = form.ai_models.length === 0
    const models = seedModels ? [...p.suggested_models] : form.ai_models
    setForm({
      ...form,
      ai_provider: p.provider,
      rest_base_url: p.base_url,
      rest_auth_type: p.auth_type,
      rest_auth_param: p.auth_param || '',
      app_callable: true,
      // LLM generations regularly outrun the generic 30s default.
      default_timeout_seconds: 120,
      ai_default_headers: p.default_headers || {},
      ai_default_query: p.default_query || {},
      ai_chat_path: p.chat_path || '',
      ai_models_path: p.models_path || '',
      ai_api_format: p.api_format || '',
      ai_models: models,
      ai_default_model: form.ai_default_model || (seedModels ? models[0] || '' : ''),
    })
  }

  const addModel = (raw: string) => {
    const m = raw.trim()
    setModelInput('')
    if (!m || form.ai_models.includes(m)) return
    setForm({ ...form, ai_models: [...form.ai_models, m] })
  }

  const removeModel = (m: string) =>
    setForm({
      ...form,
      ai_models: form.ai_models.filter((x) => x !== m),
      ai_default_model: form.ai_default_model === m ? '' : form.ai_default_model,
    })

  const handleFetchModels = async () => {
    setFetchingModels(true)
    setFetchModelsError(null)
    try {
      const res = await apiClient.post<{ models: string[] }>('/admin/connections/fetch-models', {
        config: buildConfig(form),
        credential_secret_ref: form.credential_secret_ref || null,
      })
      // Functional update — the closed-over `form` is stale after the await and
      // would revert any edit made while the fetch was in flight.
      setForm((f) => ({
        ...f,
        ai_models: res.models,
        ai_default_model: res.models.includes(f.ai_default_model) ? f.ai_default_model : '',
      }))
    } catch (e: unknown) {
      setFetchModelsError(errDetail(e))
    } finally {
      setFetchingModels(false)
    }
  }

  const submitDisabled =
    isSaving || !form.name || (form.kind === 'ai' && (!form.ai_provider || !form.rest_base_url))

  // Shared between the REST and AI branches (AI configs reuse the rest_* auth fields).
  const authFields = (
    <>
      <Field label="Auth type">
        <select
          value={form.rest_auth_type}
          onChange={(e) => set('rest_auth_type', e.target.value)}
          className={inputCls}
        >
          {REST_AUTH_TYPES.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
      </Field>
      {(form.rest_auth_type === 'api_key_header' || form.rest_auth_type === 'api_key_query') && (
        <Field
          label={form.rest_auth_type === 'api_key_header' ? 'Header name' : 'Query parameter name'}
        >
          <input
            value={form.rest_auth_param}
            onChange={(e) => set('rest_auth_param', e.target.value)}
            className={inputCls}
            placeholder={form.rest_auth_type === 'api_key_header' ? 'X-API-Key' : 'api_key'}
          />
        </Field>
      )}
    </>
  )

  const appCallableField = (
    <label className="flex items-start gap-2 rounded-lg border border-border bg-secondary/40 p-3 text-sm">
      <input
        type="checkbox"
        checked={form.app_callable}
        onChange={(e) => set('app_callable', e.target.checked)}
        className="mt-0.5"
      />
      <span>
        <span className="font-medium">Allow apps to call this connection</span>
        <span className="mt-0.5 block text-xs text-muted-foreground">
          Lets a generated app make free-form HTTP calls through this connection with
          <code className="mx-1">callConnection()</code> (once it's attached to the app). The base URL
          and credentials stay server-side. Use this to give apps access to an external API or LLM provider.
        </span>
      </span>
    </label>
  )

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/50 p-4">
      <div className="flex max-h-[90vh] w-full max-w-2xl flex-col rounded-lg border border-border bg-card shadow-xl">
        <div className="flex items-center justify-between border-b border-border px-6 py-4">
          <h2 className="text-lg font-semibold">
            {editingId ? 'Edit connection' : 'New connection'}
          </h2>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
            ✕
          </button>
        </div>

        <div className="flex-1 space-y-4 overflow-auto px-6 py-4">
          {/* Kind tabs — the backend rejects kind changes on update, so lock them on edit */}
          <div>
            <div className="flex gap-1 rounded-lg bg-secondary p-1">
              {(['sql', 'rest', 'ai'] as ConnectionKind[]).map((k) => (
                <button
                  key={k}
                  disabled={editingId !== null}
                  onClick={() =>
                    // AI connections exist to be called by apps (default app_callable
                    // ON) and LLM calls outrun the generic 30s timeout default.
                    k === 'ai'
                      ? setForm({ ...form, kind: k, app_callable: true, default_timeout_seconds: 120 })
                      : set('kind', k)
                  }
                  className={cn(
                    'flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
                    form.kind === k ? 'bg-background shadow' : 'text-muted-foreground',
                    editingId === null && form.kind !== k && 'hover:text-foreground',
                    editingId !== null && form.kind !== k && 'opacity-50',
                  )}
                >
                  {KIND_LABELS[k]}
                </button>
              ))}
            </div>
            {editingId !== null && (
              <p className="mt-1 text-xs text-muted-foreground">Kind can't be changed after creation.</p>
            )}
          </div>

          {/* Common fields */}
          <Field label="Name">
            <input
              value={form.name}
              onChange={(e) => set('name', e.target.value)}
              className={inputCls}
              placeholder="prod-analytics-postgres"
            />
          </Field>
          <Field label="Description" optional>
            <input
              value={form.description}
              onChange={(e) => set('description', e.target.value)}
              className={inputCls}
              placeholder="Read replica for analytics queries"
            />
          </Field>

          {/* Kind-specific */}
          {form.kind === 'sql' ? (
            <>
              <Field label="Dialect">
                <select
                  value={form.sql_dialect}
                  onChange={(e) => set('sql_dialect', e.target.value)}
                  className={inputCls}
                >
                  {SQL_DIALECTS.map((d) => (
                    <option key={d.value} value={d.value}>{d.label}</option>
                  ))}
                </select>
              </Field>
              {form.sql_dialect !== 'sqlite' && (
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Host">
                    <input
                      value={form.sql_host}
                      onChange={(e) => set('sql_host', e.target.value)}
                      className={inputCls}
                      placeholder="db.example.com"
                    />
                  </Field>
                  <Field label="Port" optional>
                    <input
                      value={form.sql_port}
                      onChange={(e) => set('sql_port', e.target.value)}
                      className={inputCls}
                      placeholder="5432"
                    />
                  </Field>
                </div>
              )}
              <Field
                label={form.sql_dialect === 'sqlite' ? 'Database path' : 'Database name'}
              >
                <input
                  value={form.sql_database}
                  onChange={(e) => set('sql_database', e.target.value)}
                  className={inputCls}
                  placeholder={form.sql_dialect === 'sqlite' ? '/path/to/file.db' : 'analytics'}
                />
              </Field>
              {form.sql_dialect !== 'sqlite' && (
                <Field label="Username" optional>
                  <input
                    value={form.sql_username}
                    onChange={(e) => set('sql_username', e.target.value)}
                    className={inputCls}
                    placeholder="readonly_user"
                  />
                </Field>
              )}
            </>
          ) : form.kind === 'ai' ? (
            <>
              <Field label="Provider" hint="Pick a preset — it prefills the endpoint, auth, and suggested models.">
                {aiPresetsError ? (
                  <div className="rounded-md bg-red-500/10 px-3 py-2 text-sm text-red-400">
                    Couldn't load provider presets: {aiPresetsError}
                  </div>
                ) : !aiPresets ? (
                  <div className="flex items-center gap-2 py-2 text-sm text-muted-foreground">
                    <Loader2 size={14} className="animate-spin" /> Loading providers…
                  </div>
                ) : (
                  <div className="grid grid-cols-2 gap-2">
                    {aiPresets.map((p) => (
                      <button
                        key={p.provider}
                        type="button"
                        onClick={() => applyPreset(p)}
                        className={cn(
                          'rounded-lg border p-3 text-left transition-colors',
                          form.ai_provider === p.provider
                            ? 'border-primary bg-primary/10'
                            : 'border-border bg-secondary/40 hover:bg-secondary',
                        )}
                      >
                        <span className="block text-sm font-medium">{p.label}</span>
                        <span className="mt-0.5 block text-xs text-muted-foreground">{p.hint}</span>
                      </button>
                    ))}
                  </div>
                )}
              </Field>
              <Field
                label="Base URL"
                hint={form.ai_provider === 'azure_openai' ? selectedPreset?.hint : undefined}
              >
                <input
                  value={form.rest_base_url}
                  onChange={(e) => set('rest_base_url', e.target.value)}
                  className={inputCls}
                  placeholder="https://api.example.com/v1"
                />
              </Field>
              {authFields}
              <Field
                label="Models"
                hint="Model ids offered to apps built on this connection (shown in model pickers). Enter adds; Fetch asks the provider for its live list."
              >
                <div className="space-y-2">
                  {form.ai_models.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {form.ai_models.map((m) => (
                        <span
                          key={m}
                          className="flex items-center gap-1 rounded-md bg-secondary px-2 py-1 font-mono text-xs"
                        >
                          {m}
                          <button
                            type="button"
                            onClick={() => removeModel(m)}
                            title={`Remove ${m}`}
                            className="text-muted-foreground hover:text-foreground"
                          >
                            <X size={12} />
                          </button>
                        </span>
                      ))}
                    </div>
                  )}
                  <div className="flex gap-2">
                    <input
                      value={modelInput}
                      onChange={(e) => setModelInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault()
                          addModel(modelInput)
                        }
                      }}
                      // Commit a typed-but-not-added model id so it isn't lost on Save.
                      onBlur={() => {
                        if (modelInput.trim()) addModel(modelInput)
                      }}
                      className={inputCls}
                      placeholder="model-id"
                    />
                    <button
                      type="button"
                      onClick={() => addModel(modelInput)}
                      disabled={!modelInput.trim()}
                      className="shrink-0 rounded-lg border border-border bg-secondary px-3 py-2 text-sm font-medium hover:bg-secondary/80 disabled:opacity-50"
                    >
                      Add
                    </button>
                    <button
                      type="button"
                      onClick={handleFetchModels}
                      disabled={fetchingModels || !form.rest_base_url}
                      title="Query the provider's models endpoint using the credential secret"
                      className="flex shrink-0 items-center gap-1.5 rounded-lg border border-border bg-secondary px-3 py-2 text-sm font-medium hover:bg-secondary/80 disabled:opacity-50"
                    >
                      {fetchingModels ? (
                        <Loader2 size={14} className="animate-spin" />
                      ) : (
                        <RefreshCw size={14} />
                      )}
                      Fetch models
                    </button>
                  </div>
                  {fetchModelsError && (
                    <div className="rounded-md bg-red-500/10 px-3 py-2 text-xs text-red-400">
                      {fetchModelsError}
                    </div>
                  )}
                  {selectedPreset &&
                    selectedPreset.suggested_models.filter((m) => !form.ai_models.includes(m)).length > 0 && (
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="text-xs text-muted-foreground">Suggestions:</span>
                        {selectedPreset.suggested_models
                          .filter((m) => !form.ai_models.includes(m))
                          .map((m) => (
                            <button
                              key={m}
                              type="button"
                              onClick={() => addModel(m)}
                              className="rounded-md border border-dashed border-border px-2 py-0.5 font-mono text-xs text-muted-foreground hover:border-muted-foreground hover:text-foreground"
                            >
                              + {m}
                            </button>
                          ))}
                      </div>
                    )}
                </div>
              </Field>
              <Field label="Default model" hint="Used when an app calls aiChat() without naming a model.">
                <select
                  value={form.ai_default_model}
                  onChange={(e) => set('ai_default_model', e.target.value)}
                  className={inputCls}
                  disabled={form.ai_models.length === 0}
                >
                  <option value="">— none —</option>
                  {form.ai_models.map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </select>
              </Field>
              {appCallableField}
            </>
          ) : (
            <>
              <Field label="Base URL">
                <input
                  value={form.rest_base_url}
                  onChange={(e) => set('rest_base_url', e.target.value)}
                  className={inputCls}
                  placeholder="https://api.example.com/v1"
                />
              </Field>
              {authFields}
              {appCallableField}
            </>
          )}

          {/* Credential reference — a picker over Secrets (the ref stores the NAME) */}
          <Field
            label="Credential secret name"
            optional
            hint={
              form.kind === 'ai'
                ? 'Name of the Secrets entry holding the provider API key. Create it in Admin → Secrets first.'
                : 'Name of an entry in Secrets that holds the password or token. Leave blank for unauthenticated.'
            }
          >
            {secretsError ? (
              <>
                <input
                  value={form.credential_secret_ref}
                  onChange={(e) => set('credential_secret_ref', e.target.value)}
                  className={inputCls}
                  placeholder="prod_postgres_password"
                />
                <p className="mt-1 text-xs text-muted-foreground">
                  Couldn't load the secrets list — enter the name manually.
                </p>
              </>
            ) : (
              <>
                <select
                  value={form.credential_secret_ref}
                  onChange={(e) => set('credential_secret_ref', e.target.value)}
                  className={inputCls}
                >
                  <option value="">— none —</option>
                  {/* A saved ref that's gone from Secrets must stay selectable: the
                      backend silently resolves missing secrets to None and the call
                      goes out unauthenticated — surface the dangling state instead. */}
                  {form.credential_secret_ref !== '' &&
                    !(secrets || []).some((s) => s.name === form.credential_secret_ref) && (
                      <option value={form.credential_secret_ref}>
                        {form.credential_secret_ref}
                        {secrets !== null ? ' (missing from Secrets)' : ''}
                      </option>
                    )}
                  {[...(secrets || [])]
                    .sort((a, b) => a.name.localeCompare(b.name))
                    .map((s) => (
                      <option key={s.id} value={s.name}>
                        {s.name} ({s.category}){s.is_set ? '' : ' — no value set'}
                      </option>
                    ))}
                </select>
                {secrets !== null && secrets.length === 0 && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    No secrets yet — create one in Admin → Secrets first.
                  </p>
                )}
              </>
            )}
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Default row limit">
              <input
                type="number"
                value={form.default_row_limit}
                onChange={(e) => set('default_row_limit', Number(e.target.value))}
                className={inputCls}
                min={1}
              />
            </Field>
            <Field label="Default timeout (s)">
              <input
                type="number"
                value={form.default_timeout_seconds}
                onChange={(e) => set('default_timeout_seconds', Number(e.target.value))}
                className={inputCls}
                min={1}
              />
            </Field>
          </div>

          {form.kind === 'sql' && (
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={form.read_only}
                onChange={(e) => set('read_only', e.target.checked)}
              />
              Read-only (informational — make sure the DB user actually is read-only)
            </label>
          )}

          {formError && (
            <div className="rounded-md bg-red-500/10 px-3 py-2 text-sm text-red-400">
              {formError}
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-border px-6 py-4">
          <button
            onClick={onClose}
            className="rounded-lg px-4 py-2 text-sm text-muted-foreground hover:text-foreground"
          >
            Cancel
          </button>
          <button
            onClick={() => onSubmit(true)}
            disabled={submitDisabled}
            className="rounded-lg border border-border bg-secondary px-4 py-2 text-sm font-medium hover:bg-secondary/80 disabled:opacity-50"
          >
            Save & test
          </button>
          <button
            onClick={() => onSubmit(false)}
            disabled={submitDisabled}
            className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {isSaving ? <Loader2 size={16} className="animate-spin" /> : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}

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
