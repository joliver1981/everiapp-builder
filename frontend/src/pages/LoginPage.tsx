import { useState, useEffect, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { Sparkles, Loader2 } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { apiClient } from '@/api/client'

interface SsoProvider {
  id: string
  name: string
  kind: 'saml' | 'oidc'
}

const SSO_ERROR_MESSAGES: Record<string, string> = {
  validation_failed: 'Single sign-on failed to validate. Please try again or contact your administrator.',
  no_username: 'Your identity provider did not return a username.',
  not_provisioned: 'Your account is not yet provisioned in EveriApp. Contact your administrator.',
  state_mismatch: 'Your sign-in session expired. Please try again.',
  exchange_failed: 'Single sign-on token exchange failed. Contact your administrator.',
  no_id_token: 'Your identity provider did not return an ID token.',
}

export function LoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [ssoProviders, setSsoProviders] = useState<SsoProvider[]>([])
  const login = useAuthStore((s) => s.login)
  const loginWithToken = useAuthStore((s) => s.loginWithToken)
  const navigate = useNavigate()

  // Handle the SSO redirect: /login#access_token=... or #saml_error / #oidc_error
  useEffect(() => {
    const hash = window.location.hash.replace(/^#/, '')
    if (!hash) return
    const params = new URLSearchParams(hash)
    const token = params.get('access_token')
    const ssoError = params.get('saml_error') || params.get('oidc_error')
    // Clear the fragment so the token isn't left in the address bar / history.
    window.history.replaceState(null, '', window.location.pathname + window.location.search)
    if (token) {
      loginWithToken(token)
        .then(() => navigate('/'))
        .catch(() => setError('Single sign-on session could not be established.'))
    } else if (ssoError) {
      setError(SSO_ERROR_MESSAGES[ssoError] || 'Single sign-on failed.')
    }
  }, [loginWithToken, navigate])

  // Fresh install (no admin yet) → send the user to create the admin account.
  useEffect(() => {
    apiClient
      .get<{ needs_admin: boolean }>('/setup/status')
      .then((s) => { if (s.needs_admin) navigate('/welcome', { replace: true }) })
      .catch(() => { /* ignore — show the normal login form */ })
  }, [navigate])

  // Discover enabled SAML + OIDC providers to render SSO buttons.
  useEffect(() => {
    Promise.all([
      apiClient.get<{ id: string; name: string }[]>('/auth/saml/providers').catch(() => []),
      apiClient.get<{ id: string; name: string }[]>('/auth/oidc/providers').catch(() => []),
    ]).then(([saml, oidc]) => {
      setSsoProviders([
        ...saml.map((p) => ({ ...p, kind: 'saml' as const })),
        ...oidc.map((p) => ({ ...p, kind: 'oidc' as const })),
      ])
    })
  }, [])

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    setIsLoading(true)
    try {
      await login(username, password)
      navigate('/')
    } catch {
      setError('Invalid credentials. Please try again.')
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <div className="w-full max-w-sm space-y-8">
        <div className="text-center">
          <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-primary">
            <Sparkles size={28} className="text-primary-foreground" />
          </div>
          <h1 className="mt-6 text-3xl font-semibold tracking-tight">EveriApp</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            Sign in with your organization account
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <div className="rounded-lg bg-destructive/10 px-4 py-3 text-sm text-destructive">
              {error}
            </div>
          )}

          <div className="space-y-2">
            <label htmlFor="username" className="text-sm font-medium">
              Username
            </label>
            <input
              id="username"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full rounded-lg border border-input bg-secondary px-4 py-2.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              placeholder="Enter your username"
              autoComplete="username"
              required
              autoFocus
            />
          </div>

          <div className="space-y-2">
            <label htmlFor="password" className="text-sm font-medium">
              Password
            </label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-lg border border-input bg-secondary px-4 py-2.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              placeholder="Enter your password"
              autoComplete="current-password"
              required
            />
          </div>

          <button
            type="submit"
            disabled={isLoading}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
          >
            {isLoading ? (
              <>
                <Loader2 size={16} className="animate-spin" />
                Signing in...
              </>
            ) : (
              'Sign in'
            )}
          </button>
        </form>

        {ssoProviders.length > 0 && (
          <div className="space-y-3">
            <div className="flex items-center gap-3">
              <div className="h-px flex-1 bg-border" />
              <span className="text-xs text-muted-foreground">or</span>
              <div className="h-px flex-1 bg-border" />
            </div>
            {ssoProviders.map((p) => (
              <a
                key={`${p.kind}-${p.id}`}
                href={`/api/auth/${p.kind}/${p.id}/login`}
                className="flex w-full items-center justify-center gap-2 rounded-lg border border-input bg-secondary px-4 py-2.5 text-sm font-medium text-foreground transition-colors hover:bg-accent"
              >
                Sign in with {p.name}
              </a>
            ))}
          </div>
        )}

        <p className="text-center text-xs text-muted-foreground">
          Sign in with your EveriApp account
        </p>
      </div>
    </div>
  )
}
