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
} from 'lucide-react'
import { PageHeader } from '@/components/layout/PageHeader'
import { apiClient } from '@/api/client'
import { cn } from '@/lib/utils'
import type { Connection, ConnectionKind, ConnectionTestResult } from '@/types'

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

type FormState = {
  name: string
  description: string
  kind: ConnectionKind
  credential_secret_ref: string
  default_row_limit: number
  default_timeout_seconds: number
  read_only: boolean
  // SQL
  sql_dialect: string
  sql_host: string
  sql_port: string
  sql_database: string
  sql_username: string
  // REST
  rest_base_url: string
  rest_auth_type: string
  rest_auth_param: string
}

const EMPTY_FORM: FormState = {
  name: '',
  description: '',
  kind: 'sql',
  credential_secret_ref: '',
  default_row_limit: 500000,
  default_timeout_seconds: 30,
  read_only: true,
  sql_dialect: 'sqlite',
  sql_host: '',
  sql_port: '',
  sql_database: '',
  sql_username: '',
  rest_base_url: '',
  rest_auth_type: 'none',
  rest_auth_param: '',
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

  const fetchConnections = async () => {
    setIsLoading(true)
    try {
      const data = await apiClient.get<Connection[]>('/admin/connections')
      setConnections(data)
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    fetchConnections()
  }, [])

  const openCreate = () => {
    setEditingId(null)
    setForm(EMPTY_FORM)
    setFormError(null)
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
      sql_dialect: (cfg.dialect as string) || 'sqlite',
      sql_host: (cfg.host as string) || '',
      sql_port: cfg.port ? String(cfg.port) : '',
      sql_database: (cfg.database as string) || '',
      sql_username: (cfg.username as string) || '',
      rest_base_url: (cfg.base_url as string) || '',
      rest_auth_type: (cfg.auth_type as string) || 'none',
      rest_auth_param: (cfg.auth_param as string) || '',
    })
    setShowForm(true)
  }

  const closeForm = () => {
    setShowForm(false)
    setEditingId(null)
    setForm(EMPTY_FORM)
    setFormError(null)
  }

  const buildConfig = (): Record<string, unknown> => {
    if (form.kind === 'sql') {
      const cfg: Record<string, unknown> = { dialect: form.sql_dialect }
      if (form.sql_host) cfg.host = form.sql_host
      if (form.sql_port) cfg.port = Number(form.sql_port)
      if (form.sql_database) cfg.database = form.sql_database
      if (form.sql_username) cfg.username = form.sql_username
      return cfg
    }
    const cfg: Record<string, unknown> = {
      base_url: form.rest_base_url,
      auth_type: form.rest_auth_type,
    }
    if (form.rest_auth_param) cfg.auth_param = form.rest_auth_param
    return cfg
  }

  const handleSubmit = async (andTest = false) => {
    setIsSaving(true)
    setFormError(null)
    try {
      const payload = {
        name: form.name,
        description: form.description,
        kind: form.kind,
        config: buildConfig(),
        credential_secret_ref: form.credential_secret_ref || null,
        default_row_limit: form.default_row_limit,
        default_timeout_seconds: form.default_timeout_seconds,
        read_only: form.read_only,
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
      const msg = e instanceof Error ? e.message : String(e)
      setFormError(`Save failed: ${msg}`)
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
        description="Reusable, credentialed pointers to external SQL databases and REST APIs. Datasets build on top of these."
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
  const summary =
    connection.kind === 'sql'
      ? `${(cfg.dialect as string) || '?'}${cfg.host ? ` @ ${cfg.host}` : ''}${cfg.database ? `/${cfg.database}` : ''}`
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
  onClose,
  onSubmit,
}: {
  form: FormState
  setForm: (f: FormState) => void
  editingId: string | null
  formError: string | null
  isSaving: boolean
  onClose: () => void
  onSubmit: (andTest?: boolean) => void
}) {
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) =>
    setForm({ ...form, [k]: v })

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
          {/* Kind tabs */}
          <div className="flex gap-1 rounded-lg bg-secondary p-1">
            {(['sql', 'rest'] as ConnectionKind[]).map((k) => (
              <button
                key={k}
                onClick={() => set('kind', k)}
                className={cn(
                  'flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
                  form.kind === k ? 'bg-background shadow' : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {k.toUpperCase()}
              </button>
            ))}
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
          )}

          {/* Credential reference */}
          <Field
            label="Credential secret name"
            optional
            hint="Name of an entry in Secrets that holds the password or token. Leave blank for unauthenticated."
          >
            <input
              value={form.credential_secret_ref}
              onChange={(e) => set('credential_secret_ref', e.target.value)}
              className={inputCls}
              placeholder="prod_postgres_password"
            />
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
            disabled={isSaving || !form.name}
            className="rounded-lg border border-border bg-secondary px-4 py-2 text-sm font-medium hover:bg-secondary/80 disabled:opacity-50"
          >
            Save & test
          </button>
          <button
            onClick={() => onSubmit(false)}
            disabled={isSaving || !form.name}
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
