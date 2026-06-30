import { useEffect, useState } from 'react'
import {
  CheckCircle2,
  Loader2,
  Pencil,
  Plus,
  Server,
  Terminal,
  Trash2,
  Wifi,
  WifiOff,
  XCircle,
} from 'lucide-react'

import { apiClient } from '@/api/client'
import { PageHeader } from '@/components/layout/PageHeader'
import { useDeploymentsStore, type TargetCreatePayload } from '@/stores/deploymentsStore'
import type { DeploymentTarget, Secret, TargetTestResult } from '@/types'
import { cn } from '@/lib/utils'

const EMPTY_FORM: TargetCreatePayload = {
  name: '',
  kind: 'agent',
  host: '',
  port: 8765,
  ssh_user: '',
  port_range_start: 9100,
  port_range_end: 9199,
  environment: 'dev',
  credential_secret_id: null,
  extra_config: {},
  is_active: true,
}

export function AdminDeploymentTargetsPage() {
  const { targets, isLoadingTargets, fetchTargets, createTarget, updateTarget, deleteTarget, testTarget } =
    useDeploymentsStore()

  const [secrets, setSecrets] = useState<Secret[]>([])
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)  // null = create mode
  const [form, setForm] = useState<TargetCreatePayload>(EMPTY_FORM)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [testingId, setTestingId] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<Record<string, TargetTestResult>>({})

  useEffect(() => {
    fetchTargets()
    apiClient.get<Secret[]>('/secrets').then(setSecrets).catch(() => setSecrets([]))
  }, [fetchTargets])

  const openCreate = () => {
    setEditingId(null)
    setForm(EMPTY_FORM)
    setError(null)
    setShowForm(true)
  }

  const openEdit = (t: DeploymentTarget) => {
    setEditingId(t.id)
    setForm({
      name: t.name,
      kind: t.kind,
      host: t.host,
      port: t.port,
      ssh_user: t.ssh_user || '',
      port_range_start: t.port_range_start,
      port_range_end: t.port_range_end,
      environment: t.environment,
      credential_secret_id: t.credential_secret_id,
      extra_config: t.extra_config || {},
      is_active: t.is_active,
    })
    setError(null)
    setShowForm(true)
  }

  const closeForm = () => {
    setShowForm(false)
    setEditingId(null)
    setError(null)
    setForm(EMPTY_FORM)
  }

  const handleSubmit = async () => {
    setSaving(true)
    setError(null)
    try {
      const payload = {
        ...form,
        port: Number(form.port),
        port_range_start: Number(form.port_range_start),
        port_range_end: Number(form.port_range_end),
        ssh_user: form.ssh_user || null,
        credential_secret_id: form.credential_secret_id || null,
      }
      if (editingId) {
        await updateTarget(editingId, payload)
      } else {
        await createTarget(payload)
      }
      closeForm()
    } catch (e: any) {
      setError(e?.message || `Failed to ${editingId ? 'update' : 'create'} target`)
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async (id: string) => {
    setTestingId(id)
    try {
      const result = await testTarget(id)
      setTestResult((s) => ({ ...s, [id]: result }))
    } catch (e: any) {
      setTestResult((s) => ({
        ...s,
        [id]: { ok: false, detail: e?.message || 'failed', agent_version: null, ports_used: [], ports_total: null },
      }))
    } finally {
      setTestingId(null)
    }
  }

  // Which Secret category to surface in the credential dropdown for this form's
  // current kind. agent -> agent_token, ssh -> ssh_private_key. The backend
  // validates the same thing on create/update.
  const expectedCredentialCategory = form.kind === 'agent' ? 'agent_token' : 'ssh_private_key'
  const matchingCredentialSecrets = secrets.filter((s) => s.category === expectedCredentialCategory)
  const credentialRequired = form.kind === 'agent' || form.kind === 'ssh'

  return (
    <div>
      <PageHeader
        title="Deployment Targets"
        description="Servers EveriApp can push built apps to. Each target has its own port range."
        actions={
          <button
            onClick={openCreate}
            className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          >
            <Plus size={16} />
            Add Target
          </button>
        }
      />

      <div className="p-8">
        {isLoadingTargets ? (
          <div className="flex justify-center py-12">
            <Loader2 size={24} className="animate-spin text-muted-foreground" />
          </div>
        ) : targets.length === 0 ? (
          <div className="rounded-xl border border-border bg-card p-12 text-center">
            <Server size={40} className="mx-auto text-muted-foreground/30" />
            <p className="mt-4 text-muted-foreground">No deployment targets yet.</p>
            <p className="mt-1 text-xs text-muted-foreground/70">
              Add a target running aihub-agent, or an SSH-accessible host.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {targets.map((t) => (
              <TargetRow
                key={t.id}
                target={t}
                onEdit={() => openEdit(t)}
                onTest={() => handleTest(t.id)}
                onDelete={async () => {
                  if (confirm(`Delete target "${t.name}"?`)) {
                    try { await deleteTarget(t.id) } catch (e: any) { alert(e?.message) }
                  }
                }}
                testing={testingId === t.id}
                result={testResult[t.id]}
              />
            ))}
          </div>
        )}

        {showForm && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
            <div className="w-full max-w-lg rounded-2xl border border-border bg-card p-6">
              <h2 className="text-lg font-semibold">
                {editingId ? `Edit Deployment Target` : 'Add Deployment Target'}
              </h2>

              <div className="mt-4 space-y-3">
                <Field label="Name">
                  <input
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    placeholder="prod-vm-1"
                    className={inputCls}
                  />
                </Field>

                <Field label="Kind">
                  <select
                    value={form.kind}
                    onChange={(e) => setForm({ ...form, kind: e.target.value as 'agent' | 'ssh' })}
                    className={inputCls}
                  >
                    <option value="agent">Agent (aihub-agent on target)</option>
                    <option value="ssh">SSH (Linux host with node + npx)</option>
                  </select>
                </Field>

                <div className="grid grid-cols-3 gap-3">
                  <Field label="Host" className="col-span-2">
                    <input
                      value={form.host}
                      onChange={(e) => setForm({ ...form, host: e.target.value })}
                      placeholder={form.kind === 'agent' ? 'localhost' : 'vm.example.com'}
                      className={inputCls}
                    />
                  </Field>
                  <Field label={form.kind === 'agent' ? 'Agent port' : 'SSH port'}>
                    <input
                      type="number"
                      value={form.port}
                      onChange={(e) => setForm({ ...form, port: Number(e.target.value) })}
                      className={inputCls}
                    />
                  </Field>
                </div>

                {form.kind === 'ssh' && (
                  <Field label="SSH user">
                    <input
                      value={form.ssh_user || ''}
                      onChange={(e) => setForm({ ...form, ssh_user: e.target.value })}
                      placeholder="ubuntu"
                      className={inputCls}
                    />
                  </Field>
                )}

                <div className="grid grid-cols-2 gap-3">
                  <Field label="App port range — start">
                    <input
                      type="number"
                      value={form.port_range_start}
                      onChange={(e) => setForm({ ...form, port_range_start: Number(e.target.value) })}
                      className={inputCls}
                    />
                  </Field>
                  <Field label="App port range — end">
                    <input
                      type="number"
                      value={form.port_range_end}
                      onChange={(e) => setForm({ ...form, port_range_end: Number(e.target.value) })}
                      className={inputCls}
                    />
                  </Field>
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <Field label="Environment">
                    <input
                      value={form.environment}
                      onChange={(e) => setForm({ ...form, environment: e.target.value })}
                      placeholder="dev | staging | prod"
                      className={inputCls}
                    />
                  </Field>
                  <Field label={`Credential (Secret)${credentialRequired ? ' *' : ''}`}>
                    <select
                      value={form.credential_secret_id || ''}
                      onChange={(e) =>
                        setForm({ ...form, credential_secret_id: e.target.value || null })
                      }
                      className={inputCls}
                    >
                      <option value="">— pick one —</option>
                      {matchingCredentialSecrets.map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.name}
                        </option>
                      ))}
                    </select>
                    {credentialRequired && matchingCredentialSecrets.length === 0 && (
                      <p className="mt-1 text-[11px] text-warning">
                        No <code className="rounded bg-muted px-1">{expectedCredentialCategory}</code> Secret found.
                        Add one in <span className="font-medium">Admin → Secrets</span> first.
                      </p>
                    )}
                  </Field>
                </div>

                {error && (
                  <div className="rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive">
                    {error}
                  </div>
                )}
              </div>

              <div className="mt-4 flex justify-end gap-2">
                <button
                  onClick={closeForm}
                  className="rounded-lg px-4 py-2 text-sm text-muted-foreground hover:text-foreground"
                >
                  Cancel
                </button>
                <button
                  onClick={handleSubmit}
                  disabled={
                    saving
                    || !form.name
                    || !form.host
                    || (credentialRequired && !form.credential_secret_id)
                  }
                  className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                  title={
                    credentialRequired && !form.credential_secret_id
                      ? `Pick a ${expectedCredentialCategory} Secret`
                      : undefined
                  }
                >
                  {saving && <Loader2 size={14} className="animate-spin" />}
                  {editingId ? 'Save changes' : 'Add Target'}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

const inputCls =
  'w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring'

function Field({
  label,
  className,
  children,
}: {
  label: string
  className?: string
  children: React.ReactNode
}) {
  return (
    <div className={className}>
      <label className="mb-1 block text-xs font-medium text-muted-foreground">{label}</label>
      {children}
    </div>
  )
}

function TargetRow({
  target,
  onEdit,
  onTest,
  onDelete,
  testing,
  result,
}: {
  target: DeploymentTarget
  onEdit: () => void
  onTest: () => void
  onDelete: () => void
  testing: boolean
  result?: TargetTestResult
}) {
  const status = result?.ok ?? (target.last_seen_status === 'ok' ? true : target.last_seen_status === 'error' ? false : undefined)
  return (
    <div className="rounded-xl border border-border bg-card px-6 py-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          {target.kind === 'agent' ? <Server size={18} /> : <Terminal size={18} />}
          <div>
            <div className="flex items-center gap-2">
              <span className="font-medium">{target.name}</span>
              <span className="rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                {target.environment}
              </span>
              {target.kind === 'agent' && target.agent_version && (
                <span className="rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                  agent {target.agent_version}
                </span>
              )}
              {!target.is_active && (
                <span className="rounded bg-warning/10 px-2 py-0.5 text-xs text-warning">inactive</span>
              )}
            </div>
            <div className="mt-1 text-xs text-muted-foreground">
              {target.kind} · {target.host}:{target.port} · ports {target.port_range_start}–{target.port_range_end}
            </div>
            {result && (
              <div className={cn('mt-1 text-xs', result.ok ? 'text-success' : 'text-destructive')}>
                {result.ok ? 'OK' : 'FAILED'}: {result.detail || '—'}
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {status === true && <Wifi size={14} className="text-success" />}
          {status === false && <WifiOff size={14} className="text-destructive" />}
          <button
            onClick={onTest}
            disabled={testing}
            className="flex items-center gap-1.5 rounded-lg bg-muted px-3 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            {testing ? (
              <Loader2 size={12} className="animate-spin" />
            ) : status === true ? (
              <CheckCircle2 size={12} />
            ) : status === false ? (
              <XCircle size={12} />
            ) : null}
            Test
          </button>
          <button
            onClick={onEdit}
            className="rounded-lg p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            title="Edit target"
          >
            <Pencil size={14} />
          </button>
          <button
            onClick={onDelete}
            className="rounded-lg p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
            title="Delete target"
          >
            <Trash2 size={14} />
          </button>
        </div>
      </div>
    </div>
  )
}
