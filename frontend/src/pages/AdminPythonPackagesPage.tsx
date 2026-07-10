import { useCallback, useEffect, useState } from 'react'
import {
  Package,
  Loader2,
  Trash2,
  Search,
  Download,
  RefreshCw,
  AlertTriangle,
  Hammer,
} from 'lucide-react'
import { PageHeader } from '@/components/layout/PageHeader'
import { apiClient } from '@/api/client'
import type {
  PythonPackage,
  PythonPackageLookup,
  PythonPackagesInventory,
  PythonPackageStatus,
} from '@/types'

const inputCls =
  'w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring'

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

const STATUS_STYLES: Record<PythonPackageStatus, { dot: string; label: string }> = {
  pending: { dot: 'bg-warning animate-pulse', label: 'Pending' },
  installing: { dot: 'bg-warning animate-pulse', label: 'Installing' },
  installed: { dot: 'bg-success', label: 'Installed' },
  uninstalling: { dot: 'bg-warning animate-pulse', label: 'Removing' },
  failed: { dot: 'bg-destructive', label: 'Failed' },
}

const TRANSIENT: PythonPackageStatus[] = ['pending', 'installing', 'uninstalling']

export function AdminPythonPackagesPage() {
  const [inventory, setInventory] = useState<PythonPackagesInventory | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [pageError, setPageError] = useState('')

  const fetchInventory = useCallback(async () => {
    try {
      const data = await apiClient.get<PythonPackagesInventory>('/admin/python-packages')
      setInventory(data)
      setPageError('')
    } catch (e) {
      setPageError(errDetail(e))
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchInventory()
  }, [fetchInventory])

  // Poll while any operation is in flight (install/uninstall/rebuild take
  // 10-60s in a background job) — stop as soon as everything is terminal.
  useEffect(() => {
    if (!inventory) return
    const inFlight =
      inventory.environment.busy ||
      inventory.packages.some((p) => TRANSIENT.includes(p.status))
    if (!inFlight) return
    const id = setInterval(fetchInventory, 3000)
    return () => clearInterval(id)
  }, [inventory, fetchInventory])

  const busy = inventory?.environment.busy ?? false

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Python Packages"
        description="Libraries available to app server functions"
        actions={
          <button
            onClick={() => fetchInventory()}
            className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-secondary"
          >
            <RefreshCw size={16} /> Refresh
          </button>
        }
      />
      <div className="flex-1 overflow-auto px-8 py-6">
        {pageError && (
          <div className="mb-4 rounded-lg border border-destructive/40 bg-red-500/10 px-4 py-3 text-sm text-red-400">
            {pageError}
          </div>
        )}
        {inventory && !inventory.environment.pip_available && (
          <div className="mb-4 flex items-start gap-3 rounded-lg border border-warning/40 bg-yellow-500/10 px-4 py-3 text-sm text-yellow-400">
            <AlertTriangle size={18} className="mt-0.5 shrink-0" />
            <div>
              pip is not available in this install — package installs are disabled.
              Re-run the platform installer (it vendors pip), or set AIHUB_PYTHON_DIR
              to a full Python.
            </div>
          </div>
        )}

        {isLoading && !inventory ? (
          <div className="flex h-40 items-center justify-center">
            <Loader2 className="animate-spin text-muted-foreground" />
          </div>
        ) : inventory ? (
          <div className="space-y-6">
            <AddPackageCard
              disabled={!inventory.environment.pip_available || busy}
              busy={busy}
              onStarted={fetchInventory}
            />
            <PackagesTable inventory={inventory} onChanged={fetchInventory} />
            <EnvironmentCard inventory={inventory} onChanged={fetchInventory} />
          </div>
        ) : null}
      </div>
    </div>
  )
}

function AddPackageCard({
  disabled,
  busy,
  onStarted,
}: {
  disabled: boolean
  busy: boolean
  onStarted: () => void
}) {
  const [name, setName] = useState('')
  const [lookup, setLookup] = useState<PythonPackageLookup | null>(null)
  const [lookingUp, setLookingUp] = useState(false)
  const [version, setVersion] = useState('') // '' = latest
  const [manualVersion, setManualVersion] = useState('')
  const [installing, setInstalling] = useState(false)
  const [error, setError] = useState('')

  const doLookup = async () => {
    if (!name.trim()) return
    setLookingUp(true)
    setError('')
    setLookup(null)
    setVersion('')
    try {
      const res = await apiClient.get<PythonPackageLookup>(
        `/admin/python-packages/lookup?name=${encodeURIComponent(name.trim())}`,
      )
      setLookup(res)
    } catch (e) {
      setError(errDetail(e))
    } finally {
      setLookingUp(false)
    }
  }

  const doInstall = async () => {
    if (!name.trim()) return
    setInstalling(true)
    setError('')
    try {
      const chosen = lookup?.available ? version : manualVersion.trim()
      await apiClient.post('/admin/python-packages', {
        name: name.trim(),
        version: chosen || null,
      })
      setName('')
      setLookup(null)
      setVersion('')
      setManualVersion('')
      onStarted()
    } catch (e) {
      setError(errDetail(e))
    } finally {
      setInstalling(false)
    }
  }

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h3 className="mb-3 text-sm font-medium">Add a package</h3>
      <div className="flex flex-wrap items-center gap-2">
        <div className="w-72">
          <input
            className={inputCls}
            placeholder="Exact PyPI name, e.g. scikit-learn"
            value={name}
            onChange={(e) => {
              setName(e.target.value)
              setLookup(null)
            }}
            onKeyDown={(e) => e.key === 'Enter' && doLookup()}
            disabled={disabled}
          />
        </div>
        <button
          onClick={doLookup}
          disabled={disabled || lookingUp || !name.trim()}
          className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-secondary disabled:opacity-50"
        >
          {lookingUp ? <Loader2 size={16} className="animate-spin" /> : <Search size={16} />}
          Look up
        </button>
        {lookup?.available && (
          <select
            className="rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            value={version}
            onChange={(e) => setVersion(e.target.value)}
            disabled={disabled}
          >
            <option value="">latest{lookup.latest ? ` (${lookup.latest})` : ''}</option>
            {(lookup.versions ?? []).map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        )}
        {lookup && !lookup.available && (
          <input
            className="w-40 rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            placeholder="version (optional)"
            value={manualVersion}
            onChange={(e) => setManualVersion(e.target.value)}
            disabled={disabled}
          />
        )}
        <button
          onClick={doInstall}
          disabled={disabled || installing || !name.trim()}
          className="flex items-center gap-2 rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {installing || busy ? (
            <Loader2 size={16} className="animate-spin" />
          ) : (
            <Download size={16} />
          )}
          Install
        </button>
      </div>
      {lookup?.available && lookup.summary && (
        <p className="mt-2 text-xs text-muted-foreground">{lookup.summary}</p>
      )}
      {lookup && !lookup.available && (
        <p className="mt-2 text-xs text-yellow-400">
          {lookup.error} — you can still install by typing an exact version (or leave
          blank for latest); pip resolves against the configured index.
        </p>
      )}
      {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
      <p className="mt-2 text-xs text-muted-foreground">
        Only prebuilt wheels are supported (no source builds). Installed packages become
        importable by every app's server functions on their next run — no restart needed.
      </p>
    </div>
  )
}

function PackagesTable({
  inventory,
  onChanged,
}: {
  inventory: PythonPackagesInventory
  onChanged: () => void
}) {
  const [rowError, setRowError] = useState('')
  const busy = inventory.environment.busy

  const uninstall = async (pkg: PythonPackage) => {
    if (!confirm(`Remove ${pkg.name}? Server functions importing it will start failing.`)) return
    setRowError('')
    try {
      await apiClient.delete(`/admin/python-packages/${encodeURIComponent(pkg.name)}`)
      onChanged()
    } catch (e) {
      setRowError(errDetail(e))
    }
  }

  return (
    <div className="overflow-auto rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead className="bg-secondary/50 text-xs text-muted-foreground">
          <tr>
            <th className="px-4 py-2 text-left font-medium">Package</th>
            <th className="px-4 py-2 text-left font-medium">Version</th>
            <th className="px-4 py-2 text-left font-medium">Source</th>
            <th className="px-4 py-2 text-left font-medium">Status</th>
            <th className="px-4 py-2 text-right font-medium">Actions</th>
          </tr>
        </thead>
        <tbody>
          {inventory.packages.map((pkg) => {
            const style = STATUS_STYLES[pkg.status] ?? STATUS_STYLES.failed
            return (
              <tr key={`${pkg.source}:${pkg.name}`} className="border-t border-border/40">
                <td className="px-4 py-2">
                  <div className="flex items-center gap-2">
                    <Package size={15} className="text-muted-foreground" />
                    <span className="font-medium">{pkg.name}</span>
                  </div>
                  {pkg.error && (
                    <div className="mt-1 max-w-xl whitespace-pre-wrap break-words text-xs text-red-400">
                      {pkg.error}
                    </div>
                  )}
                </td>
                <td className="px-4 py-2 text-muted-foreground">
                  {pkg.version || (pkg.pinned_version ? `(pinned ${pkg.pinned_version})` : '—')}
                </td>
                <td className="px-4 py-2">
                  {pkg.source === 'bundled' ? (
                    <span className="rounded bg-blue-500/10 px-2 py-0.5 text-xs text-blue-400">
                      bundled
                    </span>
                  ) : (
                    <span className="rounded bg-purple-500/10 px-2 py-0.5 text-xs text-purple-400">
                      admin
                    </span>
                  )}
                </td>
                <td className="px-4 py-2">
                  <span className="flex items-center gap-2">
                    <span className={`h-2 w-2 rounded-full ${style.dot}`} />
                    <span className="text-xs">{style.label}</span>
                  </span>
                </td>
                <td className="px-4 py-2 text-right">
                  {pkg.source === 'admin' && (
                    <button
                      onClick={() => uninstall(pkg)}
                      disabled={busy || TRANSIENT.includes(pkg.status)}
                      title="Uninstall"
                      className="rounded p-1.5 text-muted-foreground hover:bg-secondary hover:text-red-400 disabled:opacity-40"
                    >
                      <Trash2 size={15} />
                    </button>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {rowError && (
        <div className="border-t border-border/40 px-4 py-2 text-xs text-red-400">{rowError}</div>
      )}
    </div>
  )
}

function EnvironmentCard({
  inventory,
  onChanged,
}: {
  inventory: PythonPackagesInventory
  onChanged: () => void
}) {
  const env = inventory.environment
  const [indexUrl, setIndexUrl] = useState(env.index_url)
  const [savingIndex, setSavingIndex] = useState(false)
  const [rebuilding, setRebuilding] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => setIndexUrl(env.index_url), [env.index_url])

  const saveIndex = async () => {
    setSavingIndex(true)
    setError('')
    try {
      await apiClient.put('/admin/settings', { pip_index_url: indexUrl.trim() })
      onChanged()
    } catch (e) {
      setError(errDetail(e))
    } finally {
      setSavingIndex(false)
    }
  }

  const rebuild = async () => {
    if (
      !confirm(
        'Rebuild the package environment? All admin-installed packages are re-installed ' +
          'from scratch in one pip run (resolves version conflicts). Takes a few minutes.',
      )
    )
      return
    setRebuilding(true)
    setError('')
    try {
      await apiClient.post('/admin/python-packages/rebuild')
      onChanged()
    } catch (e) {
      setError(errDetail(e))
    } finally {
      setRebuilding(false)
    }
  }

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h3 className="mb-3 text-sm font-medium">Environment</h3>
      <div className="grid grid-cols-1 gap-2 text-xs text-muted-foreground md:grid-cols-2">
        <div>
          Python: <span className="text-foreground">{env.python_version || 'unknown'}</span>
        </div>
        <div>
          Dependencies pulled in by installs:{' '}
          <span className="text-foreground">{env.dependency_count}</span>
        </div>
        <div className="truncate" title={env.python_path}>
          Interpreter: <span className="text-foreground">{env.python_path || '—'}</span>
        </div>
        <div className="truncate" title={env.managed_dir}>
          Package directory: <span className="text-foreground">{env.managed_dir}</span>
        </div>
      </div>
      <div className="mt-4 flex flex-wrap items-end gap-2">
        <div className="w-96 max-w-full">
          <label className="mb-1 block text-xs text-muted-foreground">
            Package index URL (blank = PyPI; set for air-gapped mirrors)
          </label>
          <input
            className={inputCls}
            placeholder="https://pypi.org (default)"
            value={indexUrl}
            onChange={(e) => setIndexUrl(e.target.value)}
          />
        </div>
        <button
          onClick={saveIndex}
          disabled={savingIndex || indexUrl.trim() === env.index_url}
          className="rounded-lg border border-border px-3 py-2 text-sm hover:bg-secondary disabled:opacity-50"
        >
          {savingIndex ? <Loader2 size={16} className="animate-spin" /> : 'Save'}
        </button>
        <div className="flex-1" />
        <button
          onClick={rebuild}
          disabled={rebuilding || env.busy || !env.pip_available}
          className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-secondary disabled:opacity-50"
          title="Re-install all admin packages in one pip run — fixes version conflicts from incremental installs"
        >
          {rebuilding || env.busy ? (
            <Loader2 size={16} className="animate-spin" />
          ) : (
            <Hammer size={16} />
          )}
          Rebuild environment
        </button>
      </div>
      {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
    </div>
  )
}
