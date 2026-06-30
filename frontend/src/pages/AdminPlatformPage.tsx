import { useEffect, useState } from 'react'
import {
  Activity,
  DollarSign,
  KeyRound,
  Loader2,
  ShieldCheck,
  Settings as SettingsIcon,
  CheckCircle,
  XCircle,
  Plus,
  Trash2,
  Pencil,
  Send,
  Copy,
  Check,
  Server,
  ScrollText,
  Archive,
  Users,
} from 'lucide-react'
import { PageHeader } from '@/components/layout/PageHeader'
import { apiClient } from '@/api/client'
import { cn } from '@/lib/utils'

type Tab = 'health' | 'status' | 'cost' | 'license' | 'auth' | 'teams' | 'audit' | 'backups' | 'settings'

const inputCls =
  'w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring'

export function AdminPlatformPage() {
  const [tab, setTab] = useState<Tab>('health')
  const tabs: { key: Tab; label: string; icon: React.ReactNode }[] = [
    { key: 'health', label: 'Health', icon: <Activity size={16} /> },
    { key: 'status', label: 'System', icon: <Server size={16} /> },
    { key: 'cost', label: 'LLM Cost', icon: <DollarSign size={16} /> },
    { key: 'license', label: 'License', icon: <KeyRound size={16} /> },
    { key: 'auth', label: 'Auth Providers', icon: <ShieldCheck size={16} /> },
    { key: 'teams', label: 'Teams', icon: <Users size={16} /> },
    { key: 'audit', label: 'Audit Log', icon: <ScrollText size={16} /> },
    { key: 'backups', label: 'Backups', icon: <Archive size={16} /> },
    { key: 'settings', label: 'Settings', icon: <SettingsIcon size={16} /> },
  ]

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Platform"
        description="License, LLM cost, identity providers, connection health, and org settings."
      />
      <div className="flex gap-1 border-b border-border px-8">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={cn(
              'flex items-center gap-2 border-b-2 px-4 py-3 text-sm font-medium transition-colors',
              tab === t.key
                ? 'border-primary text-primary'
                : 'border-transparent text-muted-foreground hover:text-foreground',
            )}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-auto px-8 py-6">
        {tab === 'health' && <HealthTab />}
        {tab === 'status' && <StatusTab />}
        {tab === 'cost' && <CostTab />}
        {tab === 'license' && <LicenseTab />}
        {tab === 'auth' && <AuthProvidersTab />}
        {tab === 'teams' && <TeamsTab />}
        {tab === 'audit' && <AuditTab />}
        {tab === 'backups' && <BackupsTab />}
        {tab === 'settings' && <SettingsTab />}
      </div>
    </div>
  )
}

// Shown when a tab's fetch fails — so a tab NEVER hangs on "Loading…" forever.
function TabError({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="rounded-lg border border-red-500/30 bg-red-500/5 p-4 text-sm">
      <p className="font-medium text-red-300">Couldn't load this section</p>
      <p className="mt-1 break-words text-xs text-red-300/80">{error}</p>
      {onRetry && (
        <button onClick={onRetry} className="mt-2 rounded bg-red-500/10 px-3 py-1 text-xs text-red-200 hover:bg-red-500/20">
          Retry
        </button>
      )}
    </div>
  )
}

// --- Health ---------------------------------------------------------------
interface ConnHealth {
  id: string
  name: string
  kind: string
  ok: boolean
  message: string
  response_time_ms: number | null
}

function HealthTab() {
  const [conns, setConns] = useState<ConnHealth[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await apiClient.get<{ connections: ConnHealth[] }>('/admin/connections/health/all')
      setConns(r.connections)
    } catch (e: any) {
      setError(e?.message || 'Request failed')
      setConns([])
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => {
    run()
  }, [])

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold">Connection health</h3>
        <button onClick={run} disabled={loading}
                className="rounded-lg bg-secondary px-3 py-1.5 text-xs font-medium hover:bg-secondary/80 disabled:opacity-50">
          {loading ? <Loader2 size={14} className="animate-spin" /> : 'Refresh'}
        </button>
      </div>
      {error ? (
        <TabError error={error} onRetry={run} />
      ) : !conns ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : conns.length === 0 ? (
        <p className="text-sm text-muted-foreground">No connections configured.</p>
      ) : (
        <ul className="space-y-2">
          {conns.map((c) => (
            <li key={c.id} className="flex items-center justify-between rounded-lg border border-border bg-card p-3">
              <div className="flex items-center gap-2">
                {c.ok ? <CheckCircle size={16} className="text-green-400" />
                      : <XCircle size={16} className="text-red-400" />}
                <span className="font-medium">{c.name}</span>
                <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] uppercase text-muted-foreground">{c.kind}</span>
              </div>
              <div className="text-xs text-muted-foreground">
                {c.message}{c.response_time_ms != null && ` · ${c.response_time_ms}ms`}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// --- Cost -----------------------------------------------------------------
function CostTab() {
  const [summary, setSummary] = useState<any>(null)
  const [byUser, setByUser] = useState<any[]>([])
  const [days, setDays] = useState(30)
  const [error, setError] = useState<string | null>(null)

  const load = async (d: number) => {
    setError(null)
    try {
      const s = await apiClient.get<any>(`/admin/llm-usage/summary?days=${d}`)
      setSummary(s)
      const u = await apiClient.get<{ users: any[] }>(`/admin/llm-usage/by-user?days=${d}`)
      setByUser(u.users)
    } catch (e: any) {
      setError(e?.message || 'Request failed')
    }
  }
  useEffect(() => {
    load(days)
  }, [days])

  return (
    <div>
      <div className="mb-4 flex items-center gap-2">
        <h3 className="text-sm font-semibold">LLM cost</h3>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}
                className="rounded border border-input bg-secondary px-2 py-1 text-xs">
          <option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option>
          <option value={90}>Last 90 days</option>
        </select>
      </div>
      {error && <div className="mb-4"><TabError error={error} onRetry={() => load(days)} /></div>}
      {summary && (
        <div className="mb-6 grid grid-cols-4 gap-3">
          <Stat label="Total cost" value={`$${(summary.total_cost_usd ?? 0).toFixed(2)}`} />
          <Stat label="Calls" value={String(summary.total_calls ?? 0)} />
          <Stat label="Input tokens" value={(summary.total_input_tokens ?? 0).toLocaleString()} />
          <Stat label="Output tokens" value={(summary.total_output_tokens ?? 0).toLocaleString()} />
        </div>
      )}
      <h4 className="mb-2 text-xs font-semibold uppercase text-muted-foreground">By user</h4>
      {byUser.length === 0 ? (
        <p className="text-sm text-muted-foreground">No usage recorded yet.</p>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-muted-foreground">
            <tr><th className="py-1">User</th><th>Calls</th><th>Tokens</th><th>Cost</th></tr>
          </thead>
          <tbody>
            {byUser.map((u) => (
              <tr key={u.user_id} className="border-t border-border/40">
                <td className="py-1.5 font-mono text-xs">{u.user_id.slice(0, 8)}…</td>
                <td>{u.calls}</td>
                <td>{u.tokens.toLocaleString()}</td>
                <td>${u.cost_usd.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="text-lg font-semibold">{value}</div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  )
}

// --- License --------------------------------------------------------------
function LicenseTab() {
  const [info, setInfo] = useState<any>(null)
  const [token, setToken] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    setError(null)
    try {
      setInfo(await apiClient.get<any>('/admin/license'))
    } catch (e: any) {
      setError(e?.message || 'Request failed')
    }
  }
  useEffect(() => {
    load()
  }, [])

  const install = async () => {
    setMsg(null)
    try {
      const r = await apiClient.post<any>('/admin/license', { token })
      setInfo(r)
      setToken('')
      setMsg('License installed.')
    } catch (e: any) {
      setMsg(`Rejected: ${e.message}`)
    }
  }

  return (
    <div className="max-w-2xl">
      <h3 className="mb-3 text-sm font-semibold">Current license</h3>
      {error ? (
        <div className="mb-6"><TabError error={error} onRetry={load} /></div>
      ) : info ? (
        <div className="mb-6 rounded-lg border border-border bg-card p-4 text-sm">
          <Row k="Customer" v={info.sub} />
          <Row k="Tier" v={info.tier} />
          <Row k="Status" v={info.status} />
          <Row k="Seats" v={info.seats === 0 ? 'unlimited' : String(info.seats)} />
          <Row k="Expires" v={info.is_perpetual ? 'never (perpetual)' : `${info.days_remaining} days`} />
          {info.issue && <p className="mt-2 text-xs text-yellow-400">{info.issue}</p>}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">Loading…</p>
      )}
      <h3 className="mb-2 text-sm font-semibold">Install a license key</h3>
      <textarea value={token} onChange={(e) => setToken(e.target.value)}
                placeholder="Paste the license JWT here"
                className={cn(inputCls, 'min-h-[80px] font-mono text-xs')} />
      <div className="mt-2 flex items-center gap-3">
        <button onClick={install} disabled={!token}
                className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
          Install
        </button>
        {msg && <span className="text-xs text-muted-foreground">{msg}</span>}
      </div>
    </div>
  )
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between border-b border-border/30 py-1 last:border-0">
      <span className="text-muted-foreground">{k}</span>
      <span className="font-medium">{v}</span>
    </div>
  )
}

// --- Shared form helpers --------------------------------------------------
function Toggle({ label, hint, checked, onChange }: {
  label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void
}) {
  return (
    <label className="flex cursor-pointer items-start gap-3">
      <input type="checkbox" checked={!!checked} onChange={(e) => onChange(e.target.checked)} className="mt-1" />
      <div>
        <div className="text-sm font-medium">{label}</div>
        {hint && <div className="text-xs text-muted-foreground">{hint}</div>}
      </div>
    </label>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-3 rounded-lg border border-border bg-card p-4">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h4>
      {children}
    </div>
  )
}

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      onClick={() => { navigator.clipboard?.writeText(value); setCopied(true); setTimeout(() => setCopied(false), 1200) }}
      className="text-muted-foreground hover:text-foreground"
      title="Copy"
    >
      {copied ? <Check size={13} className="text-green-400" /> : <Copy size={13} />}
    </button>
  )
}

function Labeled({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="text-sm font-medium">{label}</label>
      {hint && <p className="mb-1 text-xs text-muted-foreground">{hint}</p>}
      {children}
    </div>
  )
}

// --- Auth providers -------------------------------------------------------
function AuthProvidersTab() {
  const [providers, setProviders] = useState<any[] | null>(null)
  const [editing, setEditing] = useState<any | null>(null)  // provider object, or {} for new
  const [error, setError] = useState<string | null>(null)
  const load = async () => {
    setError(null)
    try {
      setProviders(await apiClient.get<any[]>('/admin/auth-providers'))
    } catch (e: any) {
      setError(e?.message || 'Request failed')
      setProviders([])
    }
  }
  useEffect(() => { load() }, [])

  const remove = async (id: string) => {
    if (!confirm('Delete this identity provider?')) return
    await apiClient.delete(`/admin/auth-providers/${id}`)
    load()
  }

  if (editing) {
    return <ProviderForm provider={editing} onDone={() => { setEditing(null); load() }} onCancel={() => setEditing(null)} />
  }

  return (
    <div className="max-w-3xl">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold">Identity providers (LDAP / SAML)</h3>
        <button onClick={() => setEditing({})}
                className="flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90">
          <Plus size={14} /> Add provider
        </button>
      </div>
      <p className="mb-4 text-xs text-muted-foreground">
        When a provider is enabled, logins try it first (LDAP) or offer an SSO button (SAML),
        then fall back to local auth. Group→role mapping promotes directory users automatically.
      </p>
      {error ? (
        <TabError error={error} onRetry={load} />
      ) : !providers ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : providers.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No identity providers configured. The platform uses local auth.
        </p>
      ) : (
        <ul className="space-y-2">
          {providers.map((p) => (
            <li key={p.id} className="rounded-lg border border-border bg-card p-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{p.provider_name}</span>
                  <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] uppercase text-muted-foreground">
                    {p.provider_type}
                  </span>
                  {p.is_enabled && <span className="rounded bg-green-500/10 px-1.5 py-0.5 text-[10px] text-green-400">enabled</span>}
                  {p.is_default && <span className="rounded bg-blue-500/10 px-1.5 py-0.5 text-[10px] text-blue-400">default</span>}
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => setEditing(p)} className="text-muted-foreground hover:text-foreground" title="Edit">
                    <Pencil size={14} />
                  </button>
                  <button onClick={() => remove(p.id)} className="text-muted-foreground hover:text-red-400" title="Delete">
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
              {p.provider_type === 'ldap' && (
                <p className="mt-1 font-mono text-xs text-muted-foreground">
                  {p.config?.server}{p.config?.port ? `:${p.config.port}` : ''}
                </p>
              )}
              {p.provider_type === 'saml' && (
                <div className="mt-1 flex items-center gap-2 font-mono text-xs text-muted-foreground">
                  <span className="truncate">SP metadata: {window.location.origin}/api/auth/saml/{p.id}/metadata</span>
                  <CopyButton value={`${window.location.origin}/api/auth/saml/${p.id}/metadata`} />
                </div>
              )}
              {p.provider_type === 'oidc' && (
                <div className="mt-1 flex items-center gap-2 font-mono text-xs text-muted-foreground">
                  <span className="truncate">Redirect URI: {window.location.origin}/api/auth/oidc/{p.id}/callback</span>
                  <CopyButton value={`${window.location.origin}/api/auth/oidc/${p.id}/callback`} />
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function ProviderForm({ provider, onDone, onCancel }: {
  provider: any; onDone: () => void; onCancel: () => void
}) {
  const isNew = !provider.id
  const [type, setType] = useState<string>(provider.provider_type || 'ldap')
  const [name, setName] = useState(provider.provider_name || '')
  const [defaultRole, setDefaultRole] = useState(provider.default_role || 'user')
  const [enabled, setEnabled] = useState(provider.is_enabled ?? true)
  const [isDefault, setIsDefault] = useState(provider.is_default ?? false)
  const [autoProvision, setAutoProvision] = useState(provider.auto_provision ?? true)
  const [groupMap, setGroupMap] = useState(JSON.stringify(provider.group_role_mapping || {}, null, 2))
  const [cfg, setCfg] = useState<any>(provider.config || {})
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const setC = (patch: any) => setCfg((c: any) => ({ ...c, ...patch }))

  const save = async () => {
    setError(null)
    let mapping: any
    try {
      mapping = JSON.parse(groupMap || '{}')
    } catch {
      setError('Group→role mapping must be valid JSON, e.g. {"Domain Admins": "admin"}')
      return
    }
    setSaving(true)
    try {
      const body: any = {
        provider_name: name, config: cfg, group_role_mapping: mapping,
        default_role: defaultRole, auto_provision: autoProvision,
        is_enabled: enabled, is_default: isDefault,
      }
      if (isNew) {
        body.provider_type = type
        await apiClient.post('/admin/auth-providers', body)
      } else {
        await apiClient.put(`/admin/auth-providers/${provider.id}`, body)
      }
      onDone()
    } catch (e: any) {
      setError(e?.message || 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="max-w-2xl space-y-4">
      <h3 className="text-sm font-semibold">{isNew ? 'Add identity provider' : `Edit ${provider.provider_name}`}</h3>

      <div className="grid grid-cols-2 gap-3">
        <Labeled label="Type">
          <select value={type} onChange={(e) => setType(e.target.value)} disabled={!isNew} className={inputCls}>
            <option value="ldap">LDAP / Active Directory</option>
            <option value="saml">SAML 2.0 SSO</option>
            <option value="oidc">OpenID Connect (Azure / Google / Okta)</option>
          </select>
        </Labeled>
        <Labeled label="Display name">
          <input value={name} onChange={(e) => setName(e.target.value)} className={inputCls} placeholder="Corp AD" />
        </Labeled>
      </div>

      {type === 'ldap' && (
        <Section title="LDAP connection">
          <div className="grid grid-cols-2 gap-3">
            <Labeled label="Server"><input value={cfg.server || ''} onChange={(e) => setC({ server: e.target.value })} className={inputCls} placeholder="dc01.corp.local" /></Labeled>
            <Labeled label="Port"><input type="number" value={cfg.port ?? 389} onChange={(e) => setC({ port: Number(e.target.value) })} className={inputCls} /></Labeled>
            <Labeled label="Base DN"><input value={cfg.base_dn || ''} onChange={(e) => setC({ base_dn: e.target.value })} className={inputCls} placeholder="DC=corp,DC=local" /></Labeled>
            <Labeled label="Bind template"><input value={cfg.bind_template || ''} onChange={(e) => setC({ bind_template: e.target.value })} className={inputCls} placeholder="{username}@corp.local" /></Labeled>
            <Labeled label="Bind password" hint="leave the ***REDACTED*** placeholder to keep the current value">
              <input type="password" value={cfg.bind_password || ''} onChange={(e) => setC({ bind_password: e.target.value })} className={inputCls} />
            </Labeled>
          </div>
          <Toggle label="Use SSL/TLS (LDAPS)" checked={!!cfg.use_ssl} onChange={(v) => setC({ use_ssl: v })} />
        </Section>
      )}

      {type === 'saml' && (
        <Section title="SAML identity provider">
          <Labeled label="IdP entity ID"><input value={cfg.idp_entity_id || ''} onChange={(e) => setC({ idp_entity_id: e.target.value })} className={inputCls} placeholder="https://idp.example.com/entity" /></Labeled>
          <Labeled label="IdP SSO URL"><input value={cfg.idp_sso_url || ''} onChange={(e) => setC({ idp_sso_url: e.target.value })} className={inputCls} placeholder="https://idp.example.com/sso" /></Labeled>
          <Labeled label="IdP X.509 certificate" hint="the IdP's signing certificate (PEM body)">
            <textarea value={cfg.idp_x509_cert || ''} onChange={(e) => setC({ idp_x509_cert: e.target.value })} className={cn(inputCls, 'min-h-[80px] font-mono text-xs')} />
          </Labeled>
          <Labeled label="Attribute mapping (JSON)" hint='claim → field, e.g. {"username":"...","email":"...","groups":"..."} — leave {} for defaults'>
            <textarea value={JSON.stringify(cfg.attribute_mapping || {}, null, 2)}
                      onChange={(e) => { try { setC({ attribute_mapping: JSON.parse(e.target.value || '{}') }) } catch { /* keep typing */ } }}
                      className={cn(inputCls, 'min-h-[70px] font-mono text-xs')} />
          </Labeled>
          <Labeled label="SP private key (optional)" hint="only if your IdP requires signed requests; leave ***REDACTED*** to keep">
            <textarea value={cfg.sp_private_key || ''} onChange={(e) => setC({ sp_private_key: e.target.value })} className={cn(inputCls, 'min-h-[60px] font-mono text-xs')} />
          </Labeled>
          {!isNew && (
            <div className="flex items-center gap-2 rounded bg-secondary px-2 py-1.5 text-xs">
              <span className="text-muted-foreground">SP metadata URL for your IdP:</span>
              <code className="truncate">{window.location.origin}/api/auth/saml/{provider.id}/metadata</code>
              <CopyButton value={`${window.location.origin}/api/auth/saml/${provider.id}/metadata`} />
            </div>
          )}
        </Section>
      )}

      {type === 'oidc' && (
        <Section title="OpenID Connect provider">
          <Labeled label="Discovery URL" hint="the IdP's .well-known/openid-configuration endpoint">
            <input value={cfg.discovery_url || ''} onChange={(e) => setC({ discovery_url: e.target.value })}
                   className={inputCls} placeholder="https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration" />
          </Labeled>
          <div className="grid grid-cols-2 gap-3">
            <Labeled label="Client ID"><input value={cfg.client_id || ''} onChange={(e) => setC({ client_id: e.target.value })} className={inputCls} /></Labeled>
            <Labeled label="Client secret" hint="leave ***REDACTED*** to keep">
              <input type="password" value={cfg.client_secret || ''} onChange={(e) => setC({ client_secret: e.target.value })} className={inputCls} />
            </Labeled>
          </div>
          <Labeled label="Scopes" hint="space-separated">
            <input value={cfg.scopes ?? 'openid email profile'} onChange={(e) => setC({ scopes: e.target.value })} className={inputCls} />
          </Labeled>
          <Labeled label="Attribute mapping (JSON)" hint='claim → field, e.g. {"groups":"roles"} — leave {} for defaults'>
            <textarea value={JSON.stringify(cfg.attribute_mapping || {}, null, 2)}
                      onChange={(e) => { try { setC({ attribute_mapping: JSON.parse(e.target.value || '{}') }) } catch { /* keep typing */ } }}
                      className={cn(inputCls, 'min-h-[60px] font-mono text-xs')} />
          </Labeled>
          {!isNew && (
            <div className="flex items-center gap-2 rounded bg-secondary px-2 py-1.5 text-xs">
              <span className="text-muted-foreground">Redirect URI to register at your IdP:</span>
              <code className="truncate">{window.location.origin}/api/auth/oidc/{provider.id}/callback</code>
              <CopyButton value={`${window.location.origin}/api/auth/oidc/${provider.id}/callback`} />
            </div>
          )}
        </Section>
      )}

      <Section title="Role mapping & behavior">
        <Labeled label="Group → role mapping (JSON)" hint='e.g. {"Domain Admins":"admin","Developers":"developer"} — highest role wins'>
          <textarea value={groupMap} onChange={(e) => setGroupMap(e.target.value)} className={cn(inputCls, 'min-h-[70px] font-mono text-xs')} />
        </Labeled>
        <div className="grid grid-cols-2 gap-3">
          <Labeled label="Default role" hint="when no group matches">
            <select value={defaultRole} onChange={(e) => setDefaultRole(e.target.value)} className={inputCls}>
              <option value="user">user</option>
              <option value="developer">developer</option>
              <option value="admin">admin</option>
            </select>
          </Labeled>
        </div>
        <Toggle label="Auto-provision users" hint="create a local account on first successful login" checked={autoProvision} onChange={setAutoProvision} />
        <Toggle label="Enabled" checked={enabled} onChange={setEnabled} />
        <Toggle label="Default provider" hint="tried first / shown prominently" checked={isDefault} onChange={setIsDefault} />
      </Section>

      {error && <p className="text-sm text-red-400">{error}</p>}
      <div className="flex items-center gap-3">
        <button onClick={save} disabled={saving || !name}
                className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
          {saving ? 'Saving…' : isNew ? 'Create provider' : 'Save changes'}
        </button>
        <button onClick={onCancel} className="rounded-lg border border-border px-4 py-2 text-sm hover:bg-accent">Cancel</button>
      </div>
    </div>
  )
}

// --- Settings -------------------------------------------------------------
function SettingsTab() {
  const [settings, setSettings] = useState<any>(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    setError(null)
    try {
      setSettings(await apiClient.get<any>('/admin/settings'))
    } catch (e: any) {
      setError(e?.message || 'Request failed')
    }
  }
  useEffect(() => {
    load()
  }, [])

  const set = (patch: any) => setSettings((cur: any) => ({ ...cur, ...patch }))

  const save = async () => {
    setSaved(false)
    await apiClient.put('/admin/settings', {
      custom_system_prompt: settings.custom_system_prompt,
      monthly_budget_usd: Number(settings.monthly_budget_usd),
      per_user_budget_usd: Number(settings.per_user_budget_usd),
      budget_alert_threshold: Number(settings.budget_alert_threshold),
      security_scan_enabled: !!settings.security_scan_enabled,
      security_scan_block_publish: !!settings.security_scan_block_publish,
      security_scan_block_severity: settings.security_scan_block_severity,
      runtime_probe_enabled: !!settings.runtime_probe_enabled,
      require_publish_approval: !!settings.require_publish_approval,
      auto_rollback_enabled: !!settings.auto_rollback_enabled,
      auto_rollback_fail_threshold: Number(settings.auto_rollback_fail_threshold),
      siem_enabled: !!settings.siem_enabled,
      siem_endpoint: settings.siem_endpoint,
      siem_transport: settings.siem_transport,
      siem_auth_header: settings.siem_auth_header,
      // SMTP: smtp_password may be "***REDACTED***" — the backend preserves the
      // stored value when it sees the placeholder, so we always send it as-is.
      smtp_enabled: !!settings.smtp_enabled,
      smtp_host: settings.smtp_host,
      smtp_port: Number(settings.smtp_port),
      smtp_username: settings.smtp_username,
      smtp_password: settings.smtp_password,
      smtp_use_tls: !!settings.smtp_use_tls,
      notify_from: settings.notify_from,
      notify_admin_emails: settings.notify_admin_emails,
      notify_on_publish_request: !!settings.notify_on_publish_request,
      notify_on_deploy_failure: !!settings.notify_on_deploy_failure,
      notify_on_budget: !!settings.notify_on_budget,
      notify_on_bug_report: !!settings.notify_on_bug_report,
      backup_enabled: !!settings.backup_enabled,
      backup_interval_hours: Number(settings.backup_interval_hours),
      backup_retention: Number(settings.backup_retention),
      // Marketplace: api key may be "***REDACTED***" — backend preserves it.
      marketplace_url: settings.marketplace_url,
      marketplace_api_key: settings.marketplace_api_key,
    })
    setSaved(true)
  }

  if (error) return <TabError error={error} onRetry={load} />
  if (!settings) return <p className="text-sm text-muted-foreground">Loading…</p>

  return (
    <div className="max-w-2xl space-y-6">
      <Section title="AI generation">
        <Labeled label="Custom system prompt"
                 hint="Appended to every AI generation — encode your brand colors, component library, house style.">
          <textarea
            value={settings.custom_system_prompt}
            onChange={(e) => set({ custom_system_prompt: e.target.value })}
            className={cn(inputCls, 'min-h-[100px]')}
            placeholder="Always use our teal palette and rounded-2xl cards…"
          />
        </Labeled>
      </Section>

      <Section title="LLM budget">
        <div className="grid grid-cols-3 gap-3">
          <Labeled label="Org monthly ($)" hint="0 = unlimited">
            <input type="number" value={settings.monthly_budget_usd}
                   onChange={(e) => set({ monthly_budget_usd: e.target.value })} className={inputCls} />
          </Labeled>
          <Labeled label="Per-user ($)" hint="0 = unlimited">
            <input type="number" value={settings.per_user_budget_usd}
                   onChange={(e) => set({ per_user_budget_usd: e.target.value })} className={inputCls} />
          </Labeled>
          <Labeled label="Alert threshold" hint="0..1 (0.8 = warn at 80%)">
            <input type="number" step="0.1" min="0" max="1" value={settings.budget_alert_threshold}
                   onChange={(e) => set({ budget_alert_threshold: e.target.value })} className={inputCls} />
          </Labeled>
        </div>
      </Section>

      <Section title="Security scan (generated code)">
        <Toggle label="Scan generated code before publish"
                checked={settings.security_scan_enabled} onChange={(v) => set({ security_scan_enabled: v })} />
        <Toggle label="Block publishing on findings"
                hint="when off, findings are reported but don't stop a publish"
                checked={settings.security_scan_block_publish} onChange={(v) => set({ security_scan_block_publish: v })} />
        <Labeled label="Block at severity" hint="findings at or above this level block (admins can override)">
          <select value={settings.security_scan_block_severity}
                  onChange={(e) => set({ security_scan_block_severity: e.target.value })} className={inputCls}>
            {['info', 'low', 'medium', 'high', 'critical'].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </Labeled>
      </Section>

      <Section title="AI runtime verification">
        <Toggle label="Run a headless-browser check on generated apps"
                hint="renders each build in headless Chromium to catch mount/runtime errors that type-check + build can't (blank page, throws on render). Slower, and needs Playwright + Chromium installed (start.bat installs it). When off, verification stops at type-check + build + boot."
                checked={settings.runtime_probe_enabled} onChange={(v) => set({ runtime_probe_enabled: v })} />
      </Section>

      <Section title="Publishing">
        <Toggle label="Require admin approval before publish"
                hint="developers submit a publish request; admins approve or reject"
                checked={settings.require_publish_approval} onChange={(v) => set({ require_publish_approval: v })} />
      </Section>

      <Section title="Deployment auto-rollback">
        <Toggle label="Auto-rollback on repeated health failures"
                hint="redeploy the last healthy version when a deployment keeps failing its health probe"
                checked={settings.auto_rollback_enabled} onChange={(v) => set({ auto_rollback_enabled: v })} />
        <Labeled label="Failure threshold" hint="consecutive failed probes before rolling back">
          <input type="number" min="1" value={settings.auto_rollback_fail_threshold}
                 onChange={(e) => set({ auto_rollback_fail_threshold: e.target.value })} className={cn(inputCls, 'w-32')} />
        </Labeled>
      </Section>

      <Section title="Scheduled backups">
        <Toggle label="Automatic scheduled backups"
                hint="online SQLite backup + app data, kept under the data directory"
                checked={settings.backup_enabled} onChange={(v) => set({ backup_enabled: v })} />
        <div className="grid grid-cols-2 gap-3">
          <Labeled label="Interval (hours)">
            <input type="number" min="1" value={settings.backup_interval_hours ?? 24}
                   onChange={(e) => set({ backup_interval_hours: e.target.value })} className={inputCls} />
          </Labeled>
          <Labeled label="Keep newest N">
            <input type="number" min="1" value={settings.backup_retention ?? 7}
                   onChange={(e) => set({ backup_retention: e.target.value })} className={inputCls} />
          </Labeled>
        </div>
        <p className="text-xs text-muted-foreground">Manage and restore backups in the Backups tab.</p>
      </Section>

      <Section title="EveriApp Marketplace (external)">
        <Labeled label="Marketplace URL" hint="the public marketplace this server publishes to and browses, e.g. https://aihub-marketplace.vercel.app">
          <input value={settings.marketplace_url || ''} onChange={(e) => set({ marketplace_url: e.target.value })}
                 placeholder="https://aihub-marketplace.vercel.app" className={inputCls} />
        </Labeled>
        <Labeled label="Developer API key" hint="from the marketplace's Developer page; required for publishing (browse works without it)">
          <input type="password" value={settings.marketplace_api_key || ''}
                 onChange={(e) => set({ marketplace_api_key: e.target.value })} className={inputCls} />
        </Labeled>
      </Section>

      <Section title="SIEM forwarding (audit events)">
        <Toggle label="Forward audit events to a SIEM"
                checked={settings.siem_enabled} onChange={(v) => set({ siem_enabled: v })} />
        <div className="grid grid-cols-2 gap-3">
          <Labeled label="Transport">
            <select value={settings.siem_transport || 'http'} onChange={(e) => set({ siem_transport: e.target.value })} className={inputCls}>
              <option value="http">HTTP (ndjson POST)</option>
              <option value="syslog">Syslog (UDP)</option>
            </select>
          </Labeled>
          <Labeled label="Endpoint" hint="https URL, or host:port for syslog">
            <input value={settings.siem_endpoint || ''} onChange={(e) => set({ siem_endpoint: e.target.value })}
                   className={inputCls} placeholder="https://splunk.corp:8088/services/collector" />
          </Labeled>
        </div>
        <Labeled label="Auth header (optional)" hint='e.g. "Authorization: Splunk <token>"'>
          <input value={settings.siem_auth_header || ''} onChange={(e) => set({ siem_auth_header: e.target.value })} className={inputCls} />
        </Labeled>
        <SiemPanel />
      </Section>

      <Section title="Notifications (email / SMTP)">
        <Toggle label="Send email notifications"
                checked={settings.smtp_enabled} onChange={(v) => set({ smtp_enabled: v })} />
        <div className="grid grid-cols-2 gap-3">
          <Labeled label="SMTP host"><input value={settings.smtp_host || ''} onChange={(e) => set({ smtp_host: e.target.value })} className={inputCls} placeholder="smtp.corp.com" /></Labeled>
          <Labeled label="Port"><input type="number" value={settings.smtp_port ?? 587} onChange={(e) => set({ smtp_port: e.target.value })} className={inputCls} /></Labeled>
          <Labeled label="Username"><input value={settings.smtp_username || ''} onChange={(e) => set({ smtp_username: e.target.value })} className={inputCls} /></Labeled>
          <Labeled label="Password" hint="leave ***REDACTED*** to keep">
            <input type="password" value={settings.smtp_password || ''} onChange={(e) => set({ smtp_password: e.target.value })} className={inputCls} />
          </Labeled>
          <Labeled label="From address"><input value={settings.notify_from || ''} onChange={(e) => set({ notify_from: e.target.value })} className={inputCls} placeholder="aihub@corp.com" /></Labeled>
          <Labeled label="Admin recipients" hint="comma-separated; blank = all admins with an email">
            <input value={settings.notify_admin_emails || ''} onChange={(e) => set({ notify_admin_emails: e.target.value })} className={inputCls} />
          </Labeled>
        </div>
        <Toggle label="Use STARTTLS" checked={settings.smtp_use_tls} onChange={(v) => set({ smtp_use_tls: v })} />
        <div className="grid grid-cols-2 gap-1.5">
          <Toggle label="On publish request" checked={settings.notify_on_publish_request} onChange={(v) => set({ notify_on_publish_request: v })} />
          <Toggle label="On deploy failure" checked={settings.notify_on_deploy_failure} onChange={(v) => set({ notify_on_deploy_failure: v })} />
          <Toggle label="On budget breach" checked={settings.notify_on_budget} onChange={(v) => set({ notify_on_budget: v })} />
          <Toggle label="On bug report" checked={settings.notify_on_bug_report} onChange={(v) => set({ notify_on_bug_report: v })} />
        </div>
        <NotifyPanel />
      </Section>

      <div className="flex items-center gap-3">
        <button onClick={save}
                className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90">
          Save settings
        </button>
        {saved && <span className="text-xs text-green-400">Saved.</span>}
      </div>
    </div>
  )
}

function SiemPanel() {
  const [status, setStatus] = useState<any>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const load = async () => { try { setStatus(await apiClient.get<any>('/admin/siem/status')) } catch { /* ignore */ } }
  useEffect(() => { load() }, [])

  const test = async () => {
    setMsg('Testing…')
    try { await apiClient.post('/admin/siem/test'); setMsg('Test event delivered ✓') }
    catch (e: any) { setMsg(`Test failed: ${e?.message || 'error'}`) }
  }
  const flush = async () => {
    setMsg('Flushing…')
    try { const r = await apiClient.post<any>('/admin/siem/flush'); setMsg(`Forwarded ${r.forwarded ?? 0} event(s)`); load() }
    catch (e: any) { setMsg(`Flush failed: ${e?.message || 'error'}`) }
  }

  return (
    <div className="rounded-lg border border-border/60 bg-secondary/40 p-3 text-xs">
      <div className="flex items-center justify-between">
        <span className="text-muted-foreground">
          {status ? `Pending: ${status.pending} · cursor ${status.cursor?.created_at ? 'set' : 'none'}` : 'Status…'}
        </span>
        <div className="flex items-center gap-2">
          <button onClick={test} className="flex items-center gap-1 rounded bg-secondary px-2 py-1 hover:bg-secondary/70"><Send size={11} /> Test</button>
          <button onClick={flush} className="rounded bg-secondary px-2 py-1 hover:bg-secondary/70">Flush now</button>
          <button onClick={load} className="rounded bg-secondary px-2 py-1 hover:bg-secondary/70">Refresh</button>
        </div>
      </div>
      {msg && <p className="mt-2 text-muted-foreground">{msg}</p>}
      <p className="mt-1 text-muted-foreground/70">Save settings first, then Test to verify connectivity.</p>
    </div>
  )
}

function NotifyPanel() {
  const [to, setTo] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const send = async () => {
    setMsg('Sending…')
    try {
      const r = await apiClient.post<any>('/admin/notifications/test', to ? { to } : {})
      setMsg(`Sent to ${r.sent_to} ✓`)
    } catch (e: any) {
      setMsg(`Failed: ${e?.message || 'error'}`)
    }
  }
  return (
    <div className="rounded-lg border border-border/60 bg-secondary/40 p-3 text-xs">
      <div className="flex items-center gap-2">
        <input value={to} onChange={(e) => setTo(e.target.value)} placeholder="test@recipient.com (blank = your email)"
               className={cn(inputCls, 'flex-1 py-1 text-xs')} />
        <button onClick={send} className="flex items-center gap-1 rounded bg-secondary px-2 py-1 hover:bg-secondary/70">
          <Send size={11} /> Send test
        </button>
      </div>
      {msg && <p className="mt-2 text-muted-foreground">{msg}</p>}
      <p className="mt-1 text-muted-foreground/70">Save settings first, then send a test email.</p>
    </div>
  )
}

// --- System status --------------------------------------------------------
function StatusTab() {
  const [s, setS] = useState<any>(null)
  const [error, setError] = useState<string | null>(null)
  const load = () => {
    setError(null)
    apiClient.get<any>('/admin/system/status').then(setS).catch((e) => setError(e?.message || 'Request failed'))
  }
  useEffect(() => { load() }, [])
  const fmtBytes = (n: number) => {
    if (!n) return '0 B'
    const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; let v = n
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
    return `${v.toFixed(1)} ${u[i]}`
  }
  if (error) return <TabError error={error} onRetry={load} />
  if (!s) return <p className="text-sm text-muted-foreground">Loading…</p>
  const upMin = Math.floor((s.uptime_seconds || 0) / 60)
  return (
    <div className="max-w-3xl space-y-4">
      <div className="mb-1 flex items-center justify-between">
        <h3 className="text-sm font-semibold">System status</h3>
        <button onClick={load} className="rounded bg-secondary px-3 py-1.5 text-xs hover:bg-secondary/80">Refresh</button>
      </div>
      <div className="grid grid-cols-4 gap-3">
        <Stat label="Version" value={s.version} />
        <Stat label="Uptime" value={upMin >= 60 ? `${Math.floor(upMin / 60)}h ${upMin % 60}m` : `${upMin}m`} />
        <Stat label="Running apps" value={String(s.running_apps)} />
        <Stat label="DB size" value={fmtBytes(s.database?.size_bytes || 0)} />
      </div>
      <Section title="Counts">
        <div className="grid grid-cols-3 gap-2 text-sm">
          {Object.entries(s.counts || {}).map(([k, v]) => (
            <div key={k} className="flex justify-between border-b border-border/30 py-1">
              <span className="text-muted-foreground">{k.replace(/_/g, ' ')}</span>
              <span className="font-medium">{String(v)}</span>
            </div>
          ))}
        </div>
      </Section>
      <Section title="Disk">
        {s.disk?.total_bytes ? (
          <div className="text-sm">
            <div className="mb-1 flex justify-between text-xs text-muted-foreground">
              <span>{fmtBytes(s.disk.used_bytes)} used of {fmtBytes(s.disk.total_bytes)}</span>
              <span>{s.disk.percent_used}%</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded bg-secondary">
              <div className="h-full bg-primary" style={{ width: `${s.disk.percent_used}%` }} />
            </div>
          </div>
        ) : <p className="text-sm text-muted-foreground">Disk info unavailable.</p>}
      </Section>
      <Section title="Background loops">
        <div className="flex flex-wrap gap-2">
          {Object.entries(s.background_loops || {}).map(([k, on]) => (
            <span key={k} className={cn('rounded px-2 py-0.5 text-[11px]',
              on ? 'bg-green-500/10 text-green-400' : 'bg-secondary text-muted-foreground')}>
              {k.replace(/_/g, ' ')} {on ? 'on' : 'off'}
            </span>
          ))}
        </div>
      </Section>
    </div>
  )
}

// --- Audit log search -----------------------------------------------------
function AuditTab() {
  const [items, setItems] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [q, setQ] = useState('')
  const [action, setAction] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const search = async () => {
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams()
      if (q) params.set('q', q)
      if (action) params.set('action', action)
      params.set('limit', '100')
      const r = await apiClient.get<any>(`/admin/audit-logs?${params}`)
      setItems(r.items); setTotal(r.total)
    } catch (e: any) {
      setError(e?.message || 'Request failed')
    } finally { setLoading(false) }
  }
  useEffect(() => { search() }, [])

  return (
    <div className="max-w-4xl">
      <div className="mb-3 flex items-center gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && search()}
               placeholder="Search action / details / resource…" className={cn(inputCls, 'flex-1')} />
        <input value={action} onChange={(e) => setAction(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && search()}
               placeholder="action prefix" className={cn(inputCls, 'w-48')} />
        <button onClick={search} disabled={loading}
                className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
          {loading ? '…' : 'Search'}
        </button>
      </div>
      {error && <div className="mb-3"><TabError error={error} onRetry={search} /></div>}
      <p className="mb-2 text-xs text-muted-foreground">{total} event(s)</p>
      <div className="overflow-auto rounded-lg border border-border">
        <table className="w-full text-xs">
          <thead className="bg-secondary/50 text-left text-muted-foreground">
            <tr><th className="px-2 py-1.5">Time</th><th>User</th><th>Action</th><th>Resource</th><th>Details</th></tr>
          </thead>
          <tbody>
            {items.map((i) => (
              <tr key={i.id} className="border-t border-border/40 align-top">
                <td className="whitespace-nowrap px-2 py-1.5 text-muted-foreground">{i.created_at ? new Date(i.created_at).toLocaleString() : ''}</td>
                <td className="px-2 py-1.5">{i.username || i.user_id?.slice(0, 8)}</td>
                <td className="px-2 py-1.5 font-mono">{i.action}</td>
                <td className="px-2 py-1.5 text-muted-foreground">{i.resource_type}</td>
                <td className="px-2 py-1.5">{i.details}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {items.length === 0 && <p className="p-4 text-center text-sm text-muted-foreground">No events.</p>}
      </div>
    </div>
  )
}

// --- Backups --------------------------------------------------------------
function BackupsTab() {
  const [data, setData] = useState<any>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const load = () => {
    setError(null)
    apiClient.get<any>('/admin/backups').then(setData).catch((e) => setError(e?.message || 'Request failed'))
  }
  useEffect(() => { load() }, [])

  const create = async () => {
    setBusy(true); setMsg(null)
    try { const r = await apiClient.post<any>('/admin/backups'); setMsg(`Created ${r.name}`); load() }
    catch (e: any) { setMsg(`Failed: ${e?.message}`) } finally { setBusy(false) }
  }
  const restore = async (name: string) => {
    if (!confirm(`Stage a restore from ${name}? It applies on the next service restart.`)) return
    try { await apiClient.post(`/admin/backups/${encodeURIComponent(name)}/restore`); setMsg(`Restore staged — restart to apply.`); load() }
    catch (e: any) { setMsg(`Failed: ${e?.message}`) }
  }

  return (
    <div className="max-w-2xl">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold">Backups</h3>
        <button onClick={create} disabled={busy}
                className="rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
          {busy ? 'Backing up…' : 'Back up now'}
        </button>
      </div>
      {data?.pending_restore && (
        <div className="mb-3 rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-400">
          Restore staged from <code>{data.pending_restore}</code> — restart the service to apply.
        </div>
      )}
      {msg && <p className="mb-3 text-xs text-muted-foreground">{msg}</p>}
      {error ? <TabError error={error} onRetry={load} /> :
       !data ? <p className="text-sm text-muted-foreground">Loading…</p> : data.backups.length === 0 ? (
        <p className="text-sm text-muted-foreground">No backups yet.</p>
      ) : (
        <ul className="space-y-2">
          {data.backups.map((b: any) => (
            <li key={b.name} className="flex items-center justify-between rounded-lg border border-border bg-card p-3 text-sm">
              <div>
                <div className="font-mono text-xs">{b.name}</div>
                <div className="text-xs text-muted-foreground">{new Date(b.taken_at).toLocaleString()} · {(b.size_bytes / 1024 / 1024).toFixed(1)} MB</div>
              </div>
              <button onClick={() => restore(b.name)}
                      className="rounded px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-foreground">Restore</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// --- Teams ----------------------------------------------------------------
function TeamsTab() {
  const [teams, setTeams] = useState<any[]>([])
  const [users, setUsers] = useState<any[]>([])
  const [name, setName] = useState('')
  const [open, setOpen] = useState<string | null>(null)
  const [members, setMembers] = useState<any[]>([])
  const [addUser, setAddUser] = useState('')
  const [error, setError] = useState<string | null>(null)

  const load = () => {
    setError(null)
    apiClient.get<any[]>('/admin/teams').then(setTeams).catch((e) => setError(e?.message || 'Request failed'))
  }
  useEffect(() => {
    load()
    apiClient.get<any[]>('/admin/users').then(setUsers).catch(() => {})
  }, [])

  const create = async () => {
    if (!name.trim()) return
    try { await apiClient.post('/admin/teams', { name }); setName(''); load() } catch { /* dup */ }
  }
  const remove = async (id: string) => {
    if (!confirm('Delete this team?')) return
    await apiClient.delete(`/admin/teams/${id}`); if (open === id) setOpen(null); load()
  }
  const openTeam = async (id: string) => {
    setOpen(id); setMembers(await apiClient.get<any[]>(`/admin/teams/${id}/members`))
  }
  const addMember = async () => {
    if (!open || !addUser) return
    await apiClient.post(`/admin/teams/${open}/members`, { user_id: addUser })
    setMembers(await apiClient.get<any[]>(`/admin/teams/${open}/members`)); setAddUser(''); load()
  }
  const removeMember = async (uid: string) => {
    if (!open) return
    await apiClient.delete(`/admin/teams/${open}/members/${uid}`)
    setMembers(await apiClient.get<any[]>(`/admin/teams/${open}/members`)); load()
  }

  return (
    <div className="max-w-3xl">
      <p className="mb-3 text-xs text-muted-foreground">
        Teams are named groups. A team's name works as a group in app permissions — members get access to apps shared with that team.
      </p>
      <div className="mb-4 flex items-center gap-2">
        <input value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && create()}
               placeholder="New team name" className={cn(inputCls, 'flex-1')} />
        <button onClick={create} className="flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground hover:bg-primary/90">
          <Plus size={14} /> Create
        </button>
      </div>
      {error && <div className="mb-3"><TabError error={error} onRetry={load} /></div>}
      {teams.length === 0 ? <p className="text-sm text-muted-foreground">No teams yet.</p> : (
        <ul className="space-y-2">
          {teams.map((t) => (
            <li key={t.id} className="rounded-lg border border-border bg-card p-3">
              <div className="flex items-center justify-between">
                <button onClick={() => (open === t.id ? setOpen(null) : openTeam(t.id))} className="text-left">
                  <span className="text-sm font-medium">{t.name}</span>
                  <span className="ml-2 text-xs text-muted-foreground">{t.member_count} member(s)</span>
                </button>
                <button onClick={() => remove(t.id)} className="text-muted-foreground hover:text-red-400"><Trash2 size={14} /></button>
              </div>
              {open === t.id && (
                <div className="mt-3 space-y-2 border-t border-border/40 pt-3">
                  <div className="flex items-center gap-2">
                    <select value={addUser} onChange={(e) => setAddUser(e.target.value)} className={cn(inputCls, 'flex-1 py-1 text-xs')}>
                      <option value="">Add a user…</option>
                      {users.map((u) => <option key={u.id} value={u.id}>{u.username} ({u.role})</option>)}
                    </select>
                    <button onClick={addMember} disabled={!addUser} className="rounded bg-secondary px-2 py-1 text-xs hover:bg-secondary/70 disabled:opacity-50">Add</button>
                  </div>
                  {members.map((m) => (
                    <div key={m.user_id} className="flex items-center justify-between text-xs">
                      <span>{m.username} <span className="text-muted-foreground">({m.role})</span></span>
                      <button onClick={() => removeMember(m.user_id)} className="text-muted-foreground hover:text-red-400">remove</button>
                    </div>
                  ))}
                  {members.length === 0 && <p className="text-xs text-muted-foreground">No members yet.</p>}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
