import { useState, useEffect, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { Sparkles, Loader2, ShieldCheck } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { apiClient, ApiError } from '@/api/client'

/**
 * First-run screen: a fresh install has no accounts and no default credentials,
 * so the first person to open EveriApp creates the administrator account here.
 * Once an admin exists, this page bounces to /login.
 */
export function WelcomePage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [checking, setChecking] = useState(true)
  const loginWithToken = useAuthStore((s) => s.loginWithToken)
  const navigate = useNavigate()

  // If an admin already exists, this isn't a fresh install — go to login.
  useEffect(() => {
    apiClient
      .get<{ needs_admin: boolean }>('/setup/status')
      .then((s) => {
        if (!s.needs_admin) navigate('/login', { replace: true })
        else setChecking(false)
      })
      .catch(() => setChecking(false))
  }, [navigate])

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    if (password.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }
    if (password !== confirm) {
      setError('Passwords do not match.')
      return
    }
    setIsLoading(true)
    try {
      const data = await apiClient.post<{ access_token: string }>(
        '/auth/bootstrap-admin',
        { username: username.trim(), password }
      )
      await loginWithToken(data.access_token)
      navigate('/setup', { replace: true })
    } catch (err) {
      let msg = 'Could not create the admin account.'
      if (err instanceof ApiError) {
        try {
          const parsed = JSON.parse(err.message)
          if (typeof parsed.detail === 'string') msg = parsed.detail
        } catch { /* not JSON */ }
      }
      setError(msg)
    } finally {
      setIsLoading(false)
    }
  }

  if (checking) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <Loader2 size={28} className="animate-spin text-primary" />
      </div>
    )
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <div className="w-full max-w-sm space-y-8">
        <div className="text-center">
          <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-primary">
            <Sparkles size={28} className="text-primary-foreground" />
          </div>
          <h1 className="mt-6 text-3xl font-semibold tracking-tight">Welcome to EveriApp</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            Create your administrator account to get started.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <div className="rounded-lg bg-destructive/10 px-4 py-3 text-sm text-destructive">
              {error}
            </div>
          )}

          <div className="space-y-2">
            <label htmlFor="username" className="text-sm font-medium">Username</label>
            <input
              id="username"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full rounded-lg border border-input bg-secondary px-4 py-2.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              placeholder="Choose an admin username"
              autoComplete="username"
              required
              autoFocus
            />
          </div>

          <div className="space-y-2">
            <label htmlFor="password" className="text-sm font-medium">Password</label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-lg border border-input bg-secondary px-4 py-2.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              placeholder="At least 8 characters"
              autoComplete="new-password"
              required
            />
          </div>

          <div className="space-y-2">
            <label htmlFor="confirm" className="text-sm font-medium">Confirm password</label>
            <input
              id="confirm"
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              className="w-full rounded-lg border border-input bg-secondary px-4 py-2.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              placeholder="Re-enter your password"
              autoComplete="new-password"
              required
            />
          </div>

          <button
            type="submit"
            disabled={isLoading}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
          >
            {isLoading ? (
              <><Loader2 size={16} className="animate-spin" />Creating account…</>
            ) : (
              'Create admin account'
            )}
          </button>
        </form>

        <p className="flex items-center justify-center gap-1.5 text-center text-xs text-muted-foreground">
          <ShieldCheck size={12} />
          This account has full control. You can add more users later.
        </p>
      </div>
    </div>
  )
}
