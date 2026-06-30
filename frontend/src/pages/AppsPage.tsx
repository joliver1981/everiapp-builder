import { useRef, useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { PageHeader } from '@/components/layout/PageHeader'
import { useAuthStore } from '@/stores/authStore'
import { apiClient, ApiError } from '@/api/client'
import { cn } from '@/lib/utils'
import {
  AppWindow,
  Download,
  ExternalLink,
  Search,
  Loader2,
  ToggleRight,
  Hammer,
  Grid3X3,
  List,
  Sparkles,
  Trash2,
  Upload,
} from 'lucide-react'
import { ConfirmDialog } from '@/components/ConfirmDialog'

interface AppEntry {
  id: string
  name: string
  description: string
  icon: string
  status: string
  current_version: number
  ai_toggle_enabled: boolean
  created_by: string
  creator_name: string
  created_at: string
  updated_at: string
}

export function AppsPage() {
  const [apps, setApps] = useState<AppEntry[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid')
  const [deleteTarget, setDeleteTarget] = useState<AppEntry | null>(null)
  const [isDeleting, setIsDeleting] = useState(false)
  const [isImporting, setIsImporting] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const importInputRef = useRef<HTMLInputElement>(null)
  const navigate = useNavigate()
  const user = useAuthStore((s) => s.user)
  const isDeveloper = user?.role === 'admin' || user?.role === 'developer'

  useEffect(() => {
    fetchApps()
  }, [])

  const fetchApps = async () => {
    setIsLoading(true)
    try {
      const data = await apiClient.get<AppEntry[]>('/apps')
      setApps(data)
    } catch {
      setApps([])
    } finally {
      setIsLoading(false)
    }
  }

  const filteredApps = apps.filter((app) => {
    if (search) {
      const q = search.toLowerCase()
      if (!app.name.toLowerCase().includes(q) && !app.description.toLowerCase().includes(q)) {
        return false
      }
    }
    return true
  })

  const publishedApps = filteredApps.filter((a) => a.status === 'published')
  const draftApps = filteredApps.filter((a) => a.status === 'draft')

  const handleLaunch = (appId: string) => {
    window.open(`/apps/${appId}`, '_blank')
  }

  const handleEdit = (appId: string) => {
    navigate(`/builder/${appId}`)
  }

  const describeError = (err: unknown): string => {
    if (err instanceof ApiError) {
      try {
        const parsed = JSON.parse(err.message)
        if (typeof parsed.detail === 'string') return parsed.detail
      } catch { /* not JSON */ }
    }
    return err instanceof Error ? err.message : 'Something went wrong'
  }

  const handleExport = async (app: AppEntry) => {
    setActionError(null)
    try {
      const { blob, filename } = await apiClient.getBlob(`/apps/${app.id}/export`)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename ?? `${app.name}.zip`
      a.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setActionError(`Export failed: ${describeError(err)}`)
    }
  }

  const handleImportFile = async (file: File) => {
    setActionError(null)
    setIsImporting(true)
    try {
      const form = new FormData()
      form.append('file', file)
      await apiClient.postForm('/apps/import', form)
      await fetchApps()
    } catch (err) {
      setActionError(`Import failed: ${describeError(err)}`)
    } finally {
      setIsImporting(false)
    }
  }

  const handleDelete = async () => {
    if (!deleteTarget) return
    setIsDeleting(true)
    try {
      // Stop the runtime first (ignore errors — may not be running)
      try {
        await apiClient.post(`/apps/${deleteTarget.id}/runtime/stop`)
      } catch { /* ignore */ }
      await apiClient.delete(`/apps/${deleteTarget.id}`)
      setApps((prev) => prev.filter((a) => a.id !== deleteTarget.id))
      setDeleteTarget(null)
    } catch {
      // Keep dialog open on error so user can retry
    } finally {
      setIsDeleting(false)
    }
  }

  return (
    <div>
      <PageHeader
        title="My Apps"
        description={isDeveloper ? 'Your apps and published apps you have access to' : 'Apps available to you'}
      />

      <div className="p-8">
        {/* Search + view toggle */}
        <div className="mb-6 flex items-center gap-3">
          <div className="relative flex-1">
            <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full rounded-xl border border-input bg-secondary py-2.5 pl-10 pr-4 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              placeholder="Search apps..."
            />
          </div>
          {isDeveloper && (
            <>
              <input
                ref={importInputRef}
                type="file"
                accept=".zip,application/zip"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  e.target.value = '' // allow re-selecting the same file
                  if (file) handleImportFile(file)
                }}
              />
              <button
                onClick={() => importInputRef.current?.click()}
                disabled={isImporting}
                className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-2.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
                title="Import an app package (.zip)"
              >
                {isImporting ? <Loader2 size={16} className="animate-spin" /> : <Upload size={16} />}
                Import
              </button>
            </>
          )}
          <div className="flex rounded-lg border border-border">
            <button
              onClick={() => setViewMode('grid')}
              className={cn(
                'rounded-l-lg p-2 transition-colors',
                viewMode === 'grid' ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground'
              )}
            >
              <Grid3X3 size={16} />
            </button>
            <button
              onClick={() => setViewMode('list')}
              className={cn(
                'rounded-r-lg p-2 transition-colors',
                viewMode === 'list' ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground'
              )}
            >
              <List size={16} />
            </button>
          </div>
        </div>

        {actionError && (
          <div className="mb-4 rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-2.5 text-sm text-destructive">
            {actionError}
          </div>
        )}

        {isLoading ? (
          <div className="flex justify-center py-12">
            <Loader2 size={24} className="animate-spin text-muted-foreground" />
          </div>
        ) : filteredApps.length === 0 ? (
          <div className="rounded-xl border border-border bg-card p-12 text-center">
            <AppWindow size={40} className="mx-auto text-muted-foreground/30" />
            <p className="mt-4 text-muted-foreground">
              {search ? 'No apps match your search' : 'No apps available yet'}
            </p>
            {isDeveloper && !search && (
              <button
                onClick={() => navigate('/builder')}
                className="mt-4 inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
              >
                <Hammer size={16} />
                Build Your First App
              </button>
            )}
          </div>
        ) : (
          <>
            {/* Published apps */}
            {publishedApps.length > 0 && (
              <div>
                <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-muted-foreground">
                  <Sparkles size={14} />
                  Published Apps
                </h2>
                <div className={viewMode === 'grid'
                  ? 'grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3'
                  : 'space-y-2'
                }>
                  {publishedApps.map((app) => (
                    viewMode === 'grid' ? (
                      <AppCard key={app.id} app={app} onLaunch={handleLaunch} onEdit={isDeveloper ? handleEdit : undefined} onDelete={isDeveloper ? () => setDeleteTarget(app) : undefined} onExport={isDeveloper ? () => handleExport(app) : undefined} />
                    ) : (
                      <AppRow key={app.id} app={app} onLaunch={handleLaunch} onEdit={isDeveloper ? handleEdit : undefined} onDelete={isDeveloper ? () => setDeleteTarget(app) : undefined} onExport={isDeveloper ? () => handleExport(app) : undefined} />
                    )
                  ))}
                </div>
              </div>
            )}

            {/* Draft apps (developers only) */}
            {isDeveloper && draftApps.length > 0 && (
              <div className={publishedApps.length > 0 ? 'mt-8' : ''}>
                <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-muted-foreground">
                  <Hammer size={14} />
                  Drafts
                </h2>
                <div className={viewMode === 'grid'
                  ? 'grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3'
                  : 'space-y-2'
                }>
                  {draftApps.map((app) => (
                    viewMode === 'grid' ? (
                      <AppCard key={app.id} app={app} onLaunch={undefined} onEdit={handleEdit} onDelete={() => setDeleteTarget(app)} isDraft />
                    ) : (
                      <AppRow key={app.id} app={app} onLaunch={undefined} onEdit={handleEdit} onDelete={() => setDeleteTarget(app)} isDraft />
                    )
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>

      <ConfirmDialog
        open={!!deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onConfirm={handleDelete}
        title="Delete App"
        description={`Are you sure you want to delete "${deleteTarget?.name}"? All versions, files, and settings will be permanently removed. This cannot be undone.`}
        confirmLabel="Delete App"
        variant="danger"
        isLoading={isDeleting}
      />
    </div>
  )
}

// ---- Grid Card ----
function AppCard({
  app,
  onLaunch,
  onEdit,
  onDelete,
  onExport,
  isDraft,
}: {
  app: AppEntry
  onLaunch?: (id: string) => void
  onEdit?: (id: string) => void
  onDelete?: () => void
  onExport?: () => void
  isDraft?: boolean
}) {
  return (
    <div className="group relative rounded-xl border border-border bg-card p-5 transition-colors hover:border-primary/20">
      <div className="absolute right-2 top-2 flex items-center">
        {onExport && (
          <button
            onClick={(e) => { e.stopPropagation(); onExport() }}
            className="rounded-lg p-1.5 text-muted-foreground/0 transition-colors group-hover:text-muted-foreground hover:!bg-accent hover:!text-foreground"
            title="Export app package (.zip)"
          >
            <Download size={14} />
          </button>
        )}
        {onDelete && (
          <button
            onClick={(e) => { e.stopPropagation(); onDelete() }}
            className="rounded-lg p-1.5 text-muted-foreground/0 transition-colors group-hover:text-muted-foreground hover:!bg-destructive/10 hover:!text-destructive"
            title="Delete app"
          >
            <Trash2 size={14} />
          </button>
        )}
      </div>
      <div className="flex items-start justify-between">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
          <AppWindow size={20} className="text-primary" />
        </div>
        <div className="flex items-center gap-1">
          {app.ai_toggle_enabled && (
            <span className="flex items-center gap-0.5 rounded-full bg-success/10 px-2 py-0.5 text-[10px] text-success" title="AI Assistant enabled">
              <ToggleRight size={10} />
              AI
            </span>
          )}
          {isDraft ? (
            <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">Draft</span>
          ) : (
            <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] text-primary">v{app.current_version}</span>
          )}
        </div>
      </div>

      <h3 className="mt-3 text-sm font-semibold">{app.name}</h3>
      <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
        {app.description || 'No description'}
      </p>
      <p className="mt-2 text-[10px] text-muted-foreground/60">
        by {app.creator_name}
      </p>

      <div className="mt-4 flex gap-2">
        {onLaunch && (
          <button
            onClick={() => onLaunch(app.id)}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
          >
            <ExternalLink size={12} />
            Launch
          </button>
        )}
        {onEdit && (
          <button
            onClick={() => onEdit(app.id)}
            className={cn(
              'flex items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium transition-colors',
              onLaunch
                ? 'border border-border text-muted-foreground hover:bg-accent hover:text-foreground'
                : 'flex-1 bg-primary px-3 py-2 text-primary-foreground hover:bg-primary/90'
            )}
          >
            <Hammer size={12} />
            {isDraft ? 'Continue Building' : 'Edit'}
          </button>
        )}
      </div>
    </div>
  )
}

// ---- List Row ----
function AppRow({
  app,
  onLaunch,
  onEdit,
  onDelete,
  onExport,
  isDraft,
}: {
  app: AppEntry
  onLaunch?: (id: string) => void
  onEdit?: (id: string) => void
  onDelete?: () => void
  onExport?: () => void
  isDraft?: boolean
}) {
  return (
    <div className="group flex items-center justify-between rounded-xl border border-border bg-card px-5 py-3 transition-colors hover:border-primary/20">
      <div className="flex items-center gap-4">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
          <AppWindow size={16} className="text-primary" />
        </div>
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-medium">{app.name}</h3>
            {app.ai_toggle_enabled && (
              <span className="flex items-center gap-0.5 rounded-full bg-success/10 px-1.5 py-0.5 text-[9px] text-success">
                <ToggleRight size={9} />
                AI
              </span>
            )}
            {isDraft ? (
              <span className="rounded bg-muted px-1.5 py-0.5 text-[9px] text-muted-foreground">Draft</span>
            ) : (
              <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[9px] text-primary">v{app.current_version}</span>
            )}
          </div>
          <p className="text-xs text-muted-foreground">
            {app.creator_name} &middot; {new Date(app.updated_at).toLocaleDateString()}
          </p>
        </div>
      </div>

      <div className="flex items-center gap-2">
        {onLaunch && (
          <button
            onClick={() => onLaunch(app.id)}
            className="flex items-center gap-1.5 rounded-lg bg-primary px-4 py-1.5 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
          >
            <ExternalLink size={12} />
            Launch
          </button>
        )}
        {onEdit && (
          <button
            onClick={() => onEdit(app.id)}
            className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <Hammer size={12} />
            {isDraft ? 'Build' : 'Edit'}
          </button>
        )}
        {onExport && (
          <button
            onClick={(e) => { e.stopPropagation(); onExport() }}
            className="rounded-lg p-1.5 text-muted-foreground/0 transition-colors group-hover:text-muted-foreground hover:!bg-accent hover:!text-foreground"
            title="Export app package (.zip)"
          >
            <Download size={14} />
          </button>
        )}
        {onDelete && (
          <button
            onClick={(e) => { e.stopPropagation(); onDelete() }}
            className="rounded-lg p-1.5 text-muted-foreground/0 transition-colors group-hover:text-muted-foreground hover:!bg-destructive/10 hover:!text-destructive"
            title="Delete app"
          >
            <Trash2 size={14} />
          </button>
        )}
      </div>
    </div>
  )
}
