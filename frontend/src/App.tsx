import { useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate, useNavigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useAuthStore } from '@/stores/authStore'
import { Shell } from '@/components/layout/Shell'
import { LoginPage } from '@/pages/LoginPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { AppBuilderPage } from '@/pages/AppBuilderPage'
import { AppsPage } from '@/pages/AppsPage'
import { AdminUsersPage } from '@/pages/AdminUsersPage'
import { AdminSecretsPage } from '@/pages/AdminSecretsPage'
import { AdminAIProvidersPage } from '@/pages/AdminAIProvidersPage'
import { AdminConnectionsPage } from '@/pages/AdminConnectionsPage'
import { AdminDatasetsPage } from '@/pages/AdminDatasetsPage'
import { AdminPlatformPage } from '@/pages/AdminPlatformPage'
import { AdminAIFlowPage } from '@/pages/AdminAIFlowPage'
import { AdminDeploymentTargetsPage } from '@/pages/AdminDeploymentTargetsPage'
import { AdminBugReportsPage } from '@/pages/AdminBugReportsPage'
import { MarketplacePage } from '@/pages/MarketplacePage'
import { AppViewerPage } from '@/pages/AppViewerPage'
import { SetupWizardPage } from '@/pages/SetupWizardPage'
import { WelcomePage } from '@/pages/WelcomePage'
import { Loader2 } from 'lucide-react'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuthStore()

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <Loader2 size={32} className="animate-spin text-primary" />
      </div>
    )
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />
  }

  return <>{children}</>
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const user = useAuthStore((s) => s.user)
  if (user?.role !== 'admin') {
    return <Navigate to="/" replace />
  }
  return <>{children}</>
}

function DeveloperRoute({ children }: { children: React.ReactNode }) {
  const user = useAuthStore((s) => s.user)
  if (user?.role !== 'admin' && user?.role !== 'developer') {
    return <Navigate to="/" replace />
  }
  return <>{children}</>
}

function AppRoutes() {
  const { checkAuth, isLoading } = useAuthStore()

  useEffect(() => {
    checkAuth()
  }, [checkAuth])

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <Loader2 size={32} className="animate-spin text-primary" />
      </div>
    )
  }

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/welcome" element={<WelcomePage />} />
      <Route
        path="/setup"
        element={
          <ProtectedRoute>
            <AdminRoute>
              <SetupWizardPage />
            </AdminRoute>
          </ProtectedRoute>
        }
      />
      <Route
        element={
          <ProtectedRoute>
            <Shell />
          </ProtectedRoute>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route path="builder" element={<DeveloperRoute><AppBuilderPage /></DeveloperRoute>} />
        <Route path="builder/:appId" element={<DeveloperRoute><AppBuilderPage /></DeveloperRoute>} />
        <Route path="apps" element={<AppsPage />} />
        <Route path="apps/:appId/view" element={<AppViewerPage />} />
        {/* Friendly fallback: bare /apps/{id} → the launch view, so typed/shared
            URLs work even without the explicit /view suffix. */}
        <Route path="apps/:appId" element={<Navigate to="view" replace />} />
        <Route path="marketplace" element={<MarketplacePage />} />
        <Route
          path="admin/users"
          element={<AdminRoute><AdminUsersPage /></AdminRoute>}
        />
        <Route
          path="admin/secrets"
          element={<AdminRoute><AdminSecretsPage /></AdminRoute>}
        />
        <Route
          path="admin/ai-providers"
          element={<AdminRoute><AdminAIProvidersPage /></AdminRoute>}
        />
        <Route
          path="admin/connections"
          element={<AdminRoute><AdminConnectionsPage /></AdminRoute>}
        />
        <Route
          path="admin/datasets"
          element={<AdminRoute><AdminDatasetsPage /></AdminRoute>}
        />
        <Route
          path="admin/deployment-targets"
          element={<AdminRoute><AdminDeploymentTargetsPage /></AdminRoute>}
        />
        <Route
          path="admin/platform"
          element={<AdminRoute><AdminPlatformPage /></AdminRoute>}
        />
        <Route
          path="admin/ai-flow"
          element={<AdminRoute><AdminAIFlowPage /></AdminRoute>}
        />
        <Route
          path="admin/bug-reports"
          element={<DeveloperRoute><AdminBugReportsPage /></DeveloperRoute>}
        />
        {/* Catch-all inside the authed Shell — any unknown URL gets a clear
            message instead of a blank black screen. */}
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}

function NotFoundPage() {
  const navigate = useNavigate()
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
      <h1 className="text-2xl font-semibold">Page not found</h1>
      <p className="max-w-sm text-sm text-muted-foreground">
        The URL <code className="rounded bg-muted px-1.5 py-0.5 font-mono">
          {window.location.pathname}
        </code> doesn't match any known route.
      </p>
      <button
        onClick={() => navigate('/')}
        className="mt-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
      >
        Back to Dashboard
      </button>
    </div>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </QueryClientProvider>
  )
}
