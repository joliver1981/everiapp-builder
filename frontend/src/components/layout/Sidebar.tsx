import { NavLink } from 'react-router-dom'
import {
  LayoutDashboard,
  Hammer,
  AppWindow,
  Shield,
  Key,
  Bot,
  Bug,
  Server,
  Store,
  LogOut,
  Sparkles,
  Database,
  Table,
  Gauge,
  Workflow,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'
import type { Role } from '@/types'

interface NavItem {
  label: string
  to: string
  icon: React.ReactNode
  roles: Role[]
}

const navItems: NavItem[] = [
  { label: 'Dashboard', to: '/', icon: <LayoutDashboard size={20} />, roles: ['admin', 'developer', 'user'] },
  { label: 'App Builder', to: '/builder', icon: <Hammer size={20} />, roles: ['admin', 'developer'] },
  { label: 'My Apps', to: '/apps', icon: <AppWindow size={20} />, roles: ['admin', 'developer', 'user'] },
  { label: 'Marketplace', to: '/marketplace', icon: <Store size={20} />, roles: ['admin', 'developer', 'user'] },
  { label: 'Users & Roles', to: '/admin/users', icon: <Shield size={20} />, roles: ['admin'] },
  { label: 'Secrets', to: '/admin/secrets', icon: <Key size={20} />, roles: ['admin'] },
  { label: 'AI Providers', to: '/admin/ai-providers', icon: <Bot size={20} />, roles: ['admin'] },
  { label: 'Connections', to: '/admin/connections', icon: <Database size={20} />, roles: ['admin'] },
  { label: 'Datasets', to: '/admin/datasets', icon: <Table size={20} />, roles: ['admin'] },
  { label: 'Deployment Targets', to: '/admin/deployment-targets', icon: <Server size={20} />, roles: ['admin'] },
  { label: 'Platform', to: '/admin/platform', icon: <Gauge size={20} />, roles: ['admin'] },
  { label: 'AI Flow', to: '/admin/ai-flow', icon: <Workflow size={20} />, roles: ['admin'] },
  { label: 'Bug Reports', to: '/admin/bug-reports', icon: <Bug size={20} />, roles: ['admin', 'developer'] },
]

export function Sidebar() {
  const { user, logout } = useAuthStore()
  const userRole = user?.role ?? 'user'

  const visibleItems = navItems.filter((item) => item.roles.includes(userRole))

  return (
    <aside className="flex h-screen w-64 flex-col border-r border-border bg-card">
      <div className="flex items-center gap-3 px-6 py-5">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary">
          <Sparkles size={20} className="text-primary-foreground" />
        </div>
        <div>
          <h1 className="text-lg font-semibold tracking-tight">EveriApp</h1>
          <p className="text-xs text-muted-foreground">AI App Platform</p>
        </div>
      </div>

      <nav className="flex-1 space-y-1 px-3 py-4">
        {visibleItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === '/'}
            className={({ isActive }) =>
              cn(
                'flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors',
                isActive
                  ? 'bg-primary/10 text-primary'
                  : 'text-muted-foreground hover:bg-accent hover:text-foreground'
              )
            }
          >
            {item.icon}
            {item.label}
          </NavLink>
        ))}
      </nav>

      <div className="border-t border-border p-4">
        <div className="flex items-center justify-between">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">{user?.display_name}</p>
            <p className="truncate text-xs text-muted-foreground capitalize">{user?.role}</p>
          </div>
          <button
            onClick={() => logout()}
            className="rounded-lg p-2 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            title="Sign out"
          >
            <LogOut size={18} />
          </button>
        </div>
      </div>
    </aside>
  )
}
