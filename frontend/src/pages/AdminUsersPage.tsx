import { useState, useEffect } from 'react'
import { PageHeader } from '@/components/layout/PageHeader'
import {
  Users, Shield, Hammer, User as UserIcon, Loader2,
  UserCheck, UserX, Plug, CheckCircle, XCircle, Search,
  UserPlus, KeyRound, X,
} from 'lucide-react'
import { apiClient, ApiError } from '@/api/client'
import { cn } from '@/lib/utils'

function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    try {
      const parsed = JSON.parse(err.message)
      if (typeof parsed.detail === 'string') return parsed.detail
    } catch { /* not JSON */ }
  }
  return err instanceof Error ? err.message : 'Something went wrong'
}

interface UserData {
  id: string
  username: string
  display_name: string
  email: string
  role: string
  is_active: boolean
  created_at: string
}

const ROLE_CONFIG = {
  admin: { label: 'Admin', icon: Shield, color: 'text-red-400' },
  developer: { label: 'Developer', icon: Hammer, color: 'text-blue-400' },
  user: { label: 'User', icon: UserIcon, color: 'text-zinc-400' },
}

export function AdminUsersPage() {
  const [users, setUsers] = useState<UserData[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [adTestResult, setAdTestResult] = useState<{ success: boolean; message: string; info?: any } | null>(null)
  const [isTesting, setIsTesting] = useState(false)
  const [adSearchQuery, setAdSearchQuery] = useState('')
  const [adSearchResults, setAdSearchResults] = useState<any[]>([])
  const [isSearching, setIsSearching] = useState(false)
  // Create local account
  const [showCreate, setShowCreate] = useState(false)
  const [cUser, setCUser] = useState('')
  const [cPass, setCPass] = useState('')
  const [cRole, setCRole] = useState('developer')
  const [cErr, setCErr] = useState<string | null>(null)
  const [cBusy, setCBusy] = useState(false)
  // Reset password
  const [resetTarget, setResetTarget] = useState<UserData | null>(null)
  const [rPass, setRPass] = useState('')
  const [rMsg, setRMsg] = useState<string | null>(null)
  const [rBusy, setRBusy] = useState(false)

  const fetchUsers = async () => {
    setIsLoading(true)
    try {
      const data = await apiClient.get<UserData[]>('/admin/users')
      setUsers(data)
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => { fetchUsers() }, [])

  const handleCreateUser = async () => {
    setCErr(null)
    if (cPass.length < 8) { setCErr('Password must be at least 8 characters.'); return }
    setCBusy(true)
    try {
      await apiClient.post('/admin/users', { username: cUser.trim(), password: cPass, role: cRole })
      setCUser(''); setCPass(''); setCRole('developer'); setShowCreate(false)
      fetchUsers()
    } catch (e) {
      setCErr(describeError(e))
    } finally {
      setCBusy(false)
    }
  }

  const handleResetPassword = async () => {
    if (!resetTarget) return
    setRMsg(null)
    if (rPass.length < 8) { setRMsg('Password must be at least 8 characters.'); return }
    setRBusy(true)
    try {
      await apiClient.post(`/admin/users/${resetTarget.id}/reset-password`, { new_password: rPass })
      setRMsg('Password updated ✓')
      setRPass('')
      setTimeout(() => { setResetTarget(null); setRMsg(null) }, 900)
    } catch (e) {
      setRMsg(describeError(e))
    } finally {
      setRBusy(false)
    }
  }

  const handleRoleChange = async (userId: string, role: string) => {
    await apiClient.put(`/admin/users/${userId}/role`, { role })
    fetchUsers()
  }

  const handleToggleActive = async (userId: string) => {
    await apiClient.post(`/admin/users/${userId}/toggle-active`)
    fetchUsers()
  }

  const handleTestAd = async () => {
    setIsTesting(true)
    setAdTestResult(null)
    try {
      const result = await apiClient.post<{ success: boolean; message: string; info?: any }>('/admin/ad/test')
      setAdTestResult(result)
    } catch {
      setAdTestResult({ success: false, message: 'Failed to test AD connection' })
    } finally {
      setIsTesting(false)
    }
  }

  const handleAdSearch = async () => {
    if (!adSearchQuery.trim()) return
    setIsSearching(true)
    try {
      const results = await apiClient.get<any[]>(`/admin/ad/search?q=${encodeURIComponent(adSearchQuery)}`)
      setAdSearchResults(results)
    } catch {
      setAdSearchResults([])
    } finally {
      setIsSearching(false)
    }
  }

  return (
    <div>
      <PageHeader
        title="Users & Roles"
        description="Manage platform users and role assignments"
      />

      <div className="p-8 space-y-8">
        {/* AD Connection Section */}
        <div className="rounded-xl border border-border bg-card p-6">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
                <Plug size={16} className="text-primary" />
              </div>
              <div>
                <h3 className="text-sm font-semibold">Active Directory Connection</h3>
                <p className="text-xs text-muted-foreground">Test and manage your AD/LDAP integration</p>
              </div>
            </div>
            <button
              onClick={handleTestAd}
              disabled={isTesting}
              className="flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {isTesting ? <Loader2 size={12} className="animate-spin" /> : <Plug size={12} />}
              Test Connection
            </button>
          </div>

          {adTestResult && (
            <div className={cn(
              'mt-4 flex items-start gap-2 rounded-lg border p-3',
              adTestResult.success ? 'border-success/30 bg-success/5' : 'border-destructive/30 bg-destructive/5'
            )}>
              {adTestResult.success ? (
                <CheckCircle size={14} className="mt-0.5 shrink-0 text-success" />
              ) : (
                <XCircle size={14} className="mt-0.5 shrink-0 text-destructive" />
              )}
              <div>
                <p className={cn(
                  'text-xs font-medium',
                  adTestResult.success ? 'text-success' : 'text-destructive'
                )}>
                  {adTestResult.message}
                </p>
                {adTestResult.info && (
                  <div className="mt-2 space-y-1">
                    {Object.entries(adTestResult.info).map(([key, value]) => (
                      <p key={key} className="text-[10px] text-muted-foreground">
                        <span className="font-medium">{key}:</span> {String(value)}
                      </p>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* AD User Search */}
          <div className="mt-4 border-t border-border pt-4">
            <p className="mb-2 text-xs font-medium text-muted-foreground">Search AD Users</p>
            <div className="flex gap-2">
              <div className="relative flex-1">
                <Search size={12} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
                <input
                  type="text"
                  placeholder="Search by username or name..."
                  value={adSearchQuery}
                  onChange={(e) => setAdSearchQuery(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleAdSearch()}
                  className="w-full rounded-lg border border-border bg-background py-2 pl-8 pr-3 text-xs outline-none focus:ring-1 focus:ring-primary"
                />
              </div>
              <button
                onClick={handleAdSearch}
                disabled={isSearching}
                className="rounded-lg bg-muted px-3 py-2 text-xs font-medium text-muted-foreground hover:text-foreground disabled:opacity-50"
              >
                {isSearching ? <Loader2 size={12} className="animate-spin" /> : 'Search'}
              </button>
            </div>
            {adSearchResults.length > 0 && (
              <div className="mt-2 max-h-40 overflow-y-auto rounded-lg border border-border">
                {adSearchResults.map((r, i) => (
                  <div key={i} className="flex items-center gap-3 border-b border-border px-3 py-2 last:border-0">
                    <UserIcon size={12} className="shrink-0 text-muted-foreground" />
                    <div>
                      <p className="text-xs font-medium">{r.display_name || r.username}</p>
                      <p className="text-[10px] text-muted-foreground">{r.username} &middot; {r.email}</p>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Local accounts: create user */}
        <div className="rounded-xl border border-border bg-card p-6">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
                <UserPlus size={16} className="text-primary" />
              </div>
              <div>
                <h3 className="text-sm font-semibold">Local Accounts</h3>
                <p className="text-xs text-muted-foreground">Create username + password accounts for your team</p>
              </div>
            </div>
            <button
              onClick={() => { setShowCreate((v) => !v); setCErr(null) }}
              className="flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-xs font-medium text-primary-foreground hover:bg-primary/90"
            >
              <UserPlus size={12} />
              {showCreate ? 'Close' : 'Add user'}
            </button>
          </div>

          {showCreate && (
            <div className="mt-4 grid grid-cols-1 gap-3 border-t border-border pt-4 sm:grid-cols-4">
              <input
                value={cUser} onChange={(e) => setCUser(e.target.value)}
                placeholder="Username" autoComplete="off"
                className="rounded-lg border border-input bg-secondary px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-ring"
              />
              <input
                type="password" value={cPass} onChange={(e) => setCPass(e.target.value)}
                placeholder="Password (min 8)" autoComplete="new-password"
                className="rounded-lg border border-input bg-secondary px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-ring"
              />
              <select
                value={cRole} onChange={(e) => setCRole(e.target.value)}
                className="rounded-lg border border-input bg-secondary px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-ring"
              >
                <option value="developer">Developer</option>
                <option value="admin">Admin</option>
                <option value="user">User</option>
              </select>
              <button
                onClick={handleCreateUser}
                disabled={cBusy || !cUser.trim() || !cPass}
                className="flex items-center justify-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {cBusy ? <Loader2 size={12} className="animate-spin" /> : <UserPlus size={12} />}
                Create
              </button>
              {cErr && <p className="text-xs text-destructive sm:col-span-4">{cErr}</p>}
            </div>
          )}
        </div>

        {/* Users List */}
        {isLoading ? (
          <div className="flex justify-center py-12">
            <Loader2 size={24} className="animate-spin text-muted-foreground" />
          </div>
        ) : users.length === 0 ? (
          <div className="rounded-xl border border-border bg-card p-12 text-center">
            <Users size={40} className="mx-auto text-muted-foreground/30" />
            <p className="mt-4 text-muted-foreground">No users yet</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Users appear here after they log in for the first time
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {users.map((u) => {
              const roleConfig = ROLE_CONFIG[u.role as keyof typeof ROLE_CONFIG] || ROLE_CONFIG.user
              const RoleIcon = roleConfig.icon

              return (
                <div
                  key={u.id}
                  className={cn(
                    'flex items-center justify-between rounded-xl border border-border bg-card px-6 py-4',
                    !u.is_active && 'opacity-50'
                  )}
                >
                  <div className="flex items-center gap-4">
                    <div className="flex h-10 w-10 items-center justify-center rounded-full bg-muted">
                      <RoleIcon size={18} className={roleConfig.color} />
                    </div>
                    <div>
                      <div className="flex items-center gap-2">
                        <h3 className="text-sm font-medium">{u.display_name}</h3>
                        {!u.is_active && (
                          <span className="rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] text-destructive">
                            Inactive
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground">
                        {u.username} &middot; {u.email}
                      </p>
                    </div>
                  </div>

                  <div className="flex items-center gap-3">
                    <select
                      value={u.role}
                      onChange={(e) => handleRoleChange(u.id, e.target.value)}
                      className="rounded-lg border border-input bg-secondary px-3 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-ring"
                    >
                      <option value="admin">Admin</option>
                      <option value="developer">Developer</option>
                      <option value="user">User</option>
                    </select>

                    <button
                      onClick={() => { setResetTarget(u); setRPass(''); setRMsg(null) }}
                      className="rounded-lg p-2 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                      title="Reset password"
                    >
                      <KeyRound size={16} />
                    </button>

                    <button
                      onClick={() => handleToggleActive(u.id)}
                      className={cn(
                        'rounded-lg p-2 text-xs transition-colors',
                        u.is_active
                          ? 'text-muted-foreground hover:bg-destructive/10 hover:text-destructive'
                          : 'text-muted-foreground hover:bg-success/10 hover:text-success'
                      )}
                      title={u.is_active ? 'Deactivate user' : 'Activate user'}
                    >
                      {u.is_active ? <UserX size={16} /> : <UserCheck size={16} />}
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Reset-password modal */}
      {resetTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div className="relative w-full max-w-sm rounded-2xl bg-card p-6 shadow-xl">
            <button
              onClick={() => { setResetTarget(null); setRMsg(null) }}
              className="absolute right-4 top-4 rounded-lg p-1 text-muted-foreground hover:text-foreground"
            >
              <X size={16} />
            </button>
            <h3 className="text-sm font-semibold">Reset password</h3>
            <p className="mt-1 text-xs text-muted-foreground">
              Set a new password for <span className="font-medium text-foreground">{resetTarget.username}</span>.
            </p>
            <input
              type="password" value={rPass} onChange={(e) => setRPass(e.target.value)}
              placeholder="New password (min 8)" autoComplete="new-password" autoFocus
              onKeyDown={(e) => e.key === 'Enter' && handleResetPassword()}
              className="mt-4 w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
            {rMsg && <p className="mt-2 text-xs text-muted-foreground">{rMsg}</p>}
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => { setResetTarget(null); setRMsg(null) }}
                className="rounded-lg px-3 py-2 text-sm text-muted-foreground hover:text-foreground"
              >
                Cancel
              </button>
              <button
                onClick={handleResetPassword}
                disabled={rBusy || !rPass}
                className="flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {rBusy && <Loader2 size={14} className="animate-spin" />}
                Set password
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
