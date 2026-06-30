import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { PageHeader } from '@/components/layout/PageHeader'
import { useAuthStore } from '@/stores/authStore'
import { apiClient } from '@/api/client'
import {
  LayoutDashboard,
  Hammer,
  AppWindow,
  Bot,
  ExternalLink,
  Loader2,
  ArrowRight,
  Sparkles,
} from 'lucide-react'

const quickActions = [
  { label: 'Build New App', description: 'Start building with AI', icon: Hammer, to: '/builder', roles: ['admin', 'developer'] },
  { label: 'Browse Apps', description: 'View available apps', icon: AppWindow, to: '/apps', roles: ['admin', 'developer', 'user'] },
  { label: 'AI Providers', description: 'Configure AI settings', icon: Bot, to: '/admin/ai-providers', roles: ['admin'] },
] as const

interface RecentApp {
  id: string
  name: string
  status: string
  current_version: number
  creator_name: string
  updated_at: string
}

export function DashboardPage() {
  const user = useAuthStore((s) => s.user)
  const navigate = useNavigate()
  const [recentApps, setRecentApps] = useState<RecentApp[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const isDeveloper = user?.role === 'admin' || user?.role === 'developer'

  useEffect(() => {
    fetchRecentApps()
  }, [])

  const fetchRecentApps = async () => {
    try {
      const apps = await apiClient.get<RecentApp[]>('/apps')
      setRecentApps(apps.slice(0, 6))
    } catch {
      setRecentApps([])
    } finally {
      setIsLoading(false)
    }
  }

  const visibleActions = quickActions.filter((a) =>
    (a.roles as readonly string[]).includes(user?.role ?? 'user')
  )

  return (
    <div>
      <PageHeader
        title={`Welcome, ${user?.display_name ?? 'User'}`}
        description="Your AI-powered app development platform"
      />
      <div className="p-8">
        <h2 className="mb-4 text-lg font-medium">Quick Actions</h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {visibleActions.map((action) => (
            <button
              key={action.to}
              onClick={() => navigate(action.to)}
              className="flex items-start gap-4 rounded-xl border border-border bg-card p-6 text-left transition-colors hover:bg-accent"
            >
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-primary/10">
                <action.icon size={20} className="text-primary" />
              </div>
              <div>
                <h3 className="font-medium">{action.label}</h3>
                <p className="mt-1 text-sm text-muted-foreground">
                  {action.description}
                </p>
              </div>
            </button>
          ))}
        </div>

        {/* Recent Apps */}
        <div className="mt-10">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-lg font-medium">
              <Sparkles size={20} />
              Recent Apps
            </h2>
            <button
              onClick={() => navigate('/apps')}
              className="flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-primary"
            >
              View all
              <ArrowRight size={12} />
            </button>
          </div>

          {isLoading ? (
            <div className="flex justify-center py-8">
              <Loader2 size={20} className="animate-spin text-muted-foreground" />
            </div>
          ) : recentApps.length === 0 ? (
            <div className="rounded-xl border border-border bg-card p-12 text-center">
              <LayoutDashboard size={32} className="mx-auto text-muted-foreground/30" />
              <p className="mt-3 text-muted-foreground">No apps yet</p>
              <p className="mt-1 text-sm text-muted-foreground">
                {isDeveloper
                  ? 'Start building your first app to see it here'
                  : 'Published apps you have access to will appear here'}
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {recentApps.map((app) => (
                <button
                  key={app.id}
                  onClick={() => {
                    if (app.status === 'published') {
                      window.open(`/apps/${app.id}`, '_blank')
                    } else if (isDeveloper) {
                      navigate(`/builder/${app.id}`)
                    }
                  }}
                  className="flex items-center gap-3 rounded-xl border border-border bg-card px-4 py-3 text-left transition-colors hover:border-primary/20 hover:bg-accent"
                >
                  <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/10">
                    <AppWindow size={16} className="text-primary" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <h3 className="truncate text-sm font-medium">{app.name}</h3>
                      {app.status === 'published' ? (
                        <span className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 text-[9px] text-primary">
                          v{app.current_version}
                        </span>
                      ) : (
                        <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[9px] text-muted-foreground">
                          Draft
                        </span>
                      )}
                    </div>
                    <p className="text-[10px] text-muted-foreground">
                      {app.creator_name} &middot; {new Date(app.updated_at).toLocaleDateString()}
                    </p>
                  </div>
                  {app.status === 'published' && (
                    <ExternalLink size={14} className="shrink-0 text-muted-foreground" />
                  )}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
