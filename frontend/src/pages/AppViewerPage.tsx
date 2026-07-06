import { useParams, useNavigate } from 'react-router-dom'
import { useState, useEffect } from 'react'
import { apiClient } from '@/api/client'
import { ArrowLeft, Loader2, ExternalLink, Maximize2, Minimize2, AlertCircle, RefreshCw, Wand2, X } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { SetupWizardRenderer, type WizardSchema } from '@/components/wizard/SetupWizardRenderer'
import type { Deployment } from '@/types'

interface SetupStatus {
  has_wizard: boolean
  complete: boolean
  missing: { key: string; label: string; step_title: string }[]
  required_total: number
}

interface AppInfo {
  id: string
  name: string
  status: string
  current_version: number
  ai_toggle_enabled: boolean
  creator_name: string
}

interface RuntimeStatusResp {
  app_id: string
  status: 'starting' | 'running' | 'stopped' | 'error' | string
  port?: number | null
  source?: string | null
  error?: string | null
  phase?: string | null
  phase_detail?: string | null
  phase_elapsed_seconds?: number | null
}

// Friendly labels for the runtime phases the backend reports.
const PHASE_LABELS: Record<string, string> = {
  queued: 'Queued',
  installing: 'Installing npm dependencies',
  spawning: 'Starting Vite dev server',
  waiting: 'Waiting for server to come up',
  running: 'Running',
  failed: 'Failed',
}

export function AppViewerPage() {
  const { appId } = useParams()
  const navigate = useNavigate()
  const [app, setApp] = useState<AppInfo | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [runtimeStatus, setRuntimeStatus] = useState<'checking' | 'starting' | 'running' | 'error'>('checking')
  const [runtimeError, setRuntimeError] = useState<string | null>(null)
  const [runtimePort, setRuntimePort] = useState<number | null>(null)
  const [deployedUrl, setDeployedUrl] = useState<string | null>(null)
  // Live progress while status='starting' — populated from /runtime/status poll
  const [runtimePhase, setRuntimePhase] = useState<string | null>(null)
  const [runtimePhaseDetail, setRuntimePhaseDetail] = useState<string | null>(null)
  const [runtimePhaseElapsed, setRuntimePhaseElapsed] = useState<number | null>(null)

  // Post-install setup: prompt when required wizard fields have no values yet.
  const user = useAuthStore((s) => s.user)
  // Read the token DIRECTLY each render, never via a zustand selector:
  // selectors only re-run on STORE changes, and the token lives outside the
  // store (apiClient) — a selector serves a frozen, eventually-expired value
  // long after apiClient refreshed, and the cookie below inherits it.
  const token = apiClient.getToken()
  const canConfigure = user?.role === 'admin' || user?.role === 'developer'
  const [setupStatus, setSetupStatus] = useState<SetupStatus | null>(null)
  const [setupWizard, setSetupWizard] = useState<WizardSchema | null>(null)
  const [showSetupModal, setShowSetupModal] = useState(false)
  const [setupError, setSetupError] = useState<string | null>(null)
  const [iframeKey, setIframeKey] = useState(0)

  useEffect(() => {
    if (!appId) return
    apiClient.get<SetupStatus>(`/apps/${appId}/setup-status`)
      .then(setSetupStatus)
      .catch(() => setSetupStatus(null)) // best-effort — never block the viewer
  }, [appId])

  const openSetupModal = async () => {
    setSetupError(null)
    try {
      const schema = await apiClient.get<WizardSchema | Record<string, never>>(`/apps/${appId}/wizard`)
      if (schema && 'steps' in schema && schema.steps?.length) {
        setSetupWizard(schema as WizardSchema)
        setShowSetupModal(true)
      } else {
        setSetupError('No setup wizard found for this app.')
      }
    } catch {
      setSetupError('Could not load the setup wizard.')
    }
  }

  const handleSetupComplete = async (values: Record<string, string | number | boolean>) => {
    try {
      const res = await apiClient.post<SetupStatus & { applied: number }>(
        `/apps/${appId}/setup`, { values },
      )
      setSetupStatus(res)
      setShowSetupModal(false)
      // Reload the app so it picks up the new config via the SDK.
      setIframeKey((k) => k + 1)
    } catch (err: any) {
      let detail = err?.message || 'Failed to save setup values'
      try {
        const parsed = JSON.parse(err.message)
        if (typeof parsed.detail === 'string') detail = parsed.detail
      } catch { /* not JSON */ }
      setSetupError(detail)
    }
  }

  // Load app info
  useEffect(() => {
    if (!appId) return
    apiClient
      .get<AppInfo>(`/apps/${appId}`)
      .then((data) => {
        if (data.status !== 'published') {
          setError('This app is not published yet')
        }
        setApp(data)
      })
      .catch(() => setError('App not found or access denied'))
      .finally(() => setIsLoading(false))
  }, [appId])

  // Record a usage event for analytics. Best-effort: never block the viewer.
  useEffect(() => {
    if (!appId) return
    apiClient.post(`/apps/${appId}/events`, { event_type: 'launch' }).catch(() => {})
  }, [appId])

  // Pick a target URL: prefer a deployment of the current published version,
  // otherwise fall back to the local Vite preview runtime.
  useEffect(() => {
    if (!app || app.status !== 'published') return

    const pickTarget = async () => {
      setRuntimeStatus('checking')
      try {
        const deployments = await apiClient
          .get<Deployment[]>(`/apps/${app.id}/deployments`)
          .catch(() => [] as Deployment[])
        const live = deployments.find(
          (d) => d.status === 'running' && d.version === app.current_version && d.public_url,
        )
        if (live?.public_url) {
          setDeployedUrl(live.public_url)
          setRuntimeStatus('running')
          return
        }

        // Fallback: spin up the local preview runtime for the published version.
        const status = await apiClient.get<RuntimeStatusResp>(`/apps/${app.id}/runtime/status`)
        applyRuntimeResp(status)
        if (status.status === 'running') return
        // Kick off the start — backend returns fast now with status='starting'
        // and we poll /runtime/status from the separate effect below.
        const resp = await apiClient.post<RuntimeStatusResp>(
          `/apps/${app.id}/runtime/start`,
          { source: `v${app.current_version}` }
        )
        applyRuntimeResp(resp)
      } catch {
        setRuntimeStatus('error')
        setRuntimeError('Failed to start app')
      }
    }

    pickTarget()
  }, [app])

  // While the runtime is starting, poll /runtime/status every 1.5s so we can
  // surface phase progress ("Installing dependencies (15s)...", "Spawning Vite..."
  // etc.) instead of an opaque spinner.
  useEffect(() => {
    if (!app || runtimeStatus !== 'starting') return
    const id = setInterval(async () => {
      try {
        const s = await apiClient.get<RuntimeStatusResp>(`/apps/${app.id}/runtime/status`)
        applyRuntimeResp(s)
      } catch {
        // transient — keep polling
      }
    }, 1500)
    return () => clearInterval(id)
  }, [app, runtimeStatus])

  // Once running, keep a slow heartbeat. Without it this page goes completely
  // quiet — zero API calls, zero re-renders — so with a 15-min access TTL the
  // in-memory token silently dies and the /apps cookie (re-set per render)
  // freezes stale; the next iframe remount ("Complete setup" reload, Open in
  // new tab, in-app refresh) then boots the app unauthenticated. Each tick is
  // an authenticated round-trip (apiClient transparently refreshes on 401) and
  // the applyRuntimeResp state change re-renders → cookie re-set fresh. It
  // also surfaces a crashed runtime instead of a stale healthy-looking page.
  useEffect(() => {
    if (!app || runtimeStatus !== 'running') return
    const id = setInterval(async () => {
      try {
        const s = await apiClient.get<RuntimeStatusResp>(`/apps/${app.id}/runtime/status`)
        applyRuntimeResp(s)
      } catch {
        // transient — keep polling
      }
    }, 10000)
    return () => clearInterval(id)
  }, [app, runtimeStatus])

  const applyRuntimeResp = (s: RuntimeStatusResp) => {
    setRuntimeStatus(s.status as any)
    if (s.port) setRuntimePort(s.port)
    setRuntimeError(s.error ?? null)
    setRuntimePhase(s.phase ?? null)
    setRuntimePhaseDetail(s.phase_detail ?? null)
    setRuntimePhaseElapsed(s.phase_elapsed_seconds ?? null)
  }

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <Loader2 size={32} className="animate-spin text-primary" />
      </div>
    )
  }

  if (error || !app) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-4">
        <p className="text-muted-foreground">{error || 'App not found'}</p>
        <button
          onClick={() => navigate('/apps')}
          className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
        >
          <ArrowLeft size={16} />
          Back to Apps
        </button>
      </div>
    )
  }

  // Local previews go through the platform runtime proxy (/apps/{id}/), NOT the
  // raw Vite port, so the proxy injects the SDK globals (window.__AIHUB_APP_ID__ /
  // __AIHUB_TOKEN__) — without them useAppConfig()/useDataset() resolve empty.
  // The token travels as an `access_token` cookie scoped to /apps (a transport
  // the proxy already accepts), NOT as a ?__aihub_token= query param: the viewer
  // is reachable by every user role, and a token in the URL would land in the
  // address bar (Open in new tab), browser history, and backend access logs.
  // Cookies are host-scoped, so the dev split-origin (5173 → 8800) still works.
  // Re-set on every render so a refreshed token reaches the next iframe request
  // without changing the iframe src (no forced reload). Deployed apps are served
  // from their target host; see app-sdk/src/useAppConfig.ts for their contract.
  if (token) {
    document.cookie = `access_token=${token}; path=/apps; SameSite=Lax` +
      (window.location.protocol === 'https:' ? '; Secure' : '')
  }
  const previewProxyUrl =
    `${import.meta.env.DEV ? 'http://localhost:8800' : ''}/apps/${app.id}/`

  return (
    <div className="flex h-screen flex-col">
      {/* Top bar (hidden in fullscreen) */}
      {!isFullscreen && (
        <div className="flex items-center justify-between border-b border-border bg-card px-4 py-2">
          <div className="flex items-center gap-3">
            <button
              onClick={() => navigate('/apps')}
              className="rounded-lg p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              <ArrowLeft size={16} />
            </button>
            <h1 className="text-sm font-semibold">{app.name}</h1>
            <span className="rounded bg-primary/10 px-2 py-0.5 text-[10px] text-primary">
              v{app.current_version}
            </span>
            {app.ai_toggle_enabled && (
              <span className="rounded-full bg-success/10 px-2 py-0.5 text-[10px] text-success">
                AI Enabled
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => window.open(deployedUrl ?? previewProxyUrl, '_blank')}
              className="rounded-lg p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              title="Open in new tab"
            >
              <ExternalLink size={14} />
            </button>
            <button
              onClick={() => setIsFullscreen(true)}
              className="rounded-lg p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              title="Fullscreen"
            >
              <Maximize2 size={14} />
            </button>
          </div>
        </div>
      )}

      {/* Setup-needed banner */}
      {!isFullscreen && setupStatus && setupStatus.has_wizard && !setupStatus.complete && (
        <div className="flex items-center justify-between gap-3 border-b border-amber-500/30 bg-amber-500/10 px-4 py-2">
          <div className="flex items-center gap-2 text-xs text-amber-700 dark:text-amber-300">
            <Wand2 size={14} />
            <span>
              This app needs setup — {setupStatus.missing.length} required setting
              {setupStatus.missing.length === 1 ? '' : 's'} missing
              {' '}({setupStatus.missing.map((m) => m.label).join(', ')}).
              {!canConfigure && ' Ask an admin or developer to complete setup.'}
            </span>
          </div>
          {canConfigure && (
            <button
              onClick={openSetupModal}
              className="shrink-0 rounded-lg bg-amber-600 px-3 py-1 text-xs font-medium text-white hover:bg-amber-700"
            >
              Complete setup
            </button>
          )}
        </div>
      )}
      {setupError && (
        <div className="border-b border-red-500/30 bg-red-500/10 px-4 py-1.5 text-xs text-red-700 dark:text-red-300">
          {setupError}
        </div>
      )}

      {/* App content */}
      <div className="relative flex-1">
        {runtimeStatus === 'running' && (deployedUrl || runtimePort) ? (
          <iframe
            key={iframeKey}
            src={deployedUrl ?? previewProxyUrl}
            className="h-full w-full border-0"
            title={app.name}
          />
        ) : runtimeStatus === 'starting' || runtimeStatus === 'checking' ? (
          <div className="flex h-full items-center justify-center">
            <div className="max-w-md text-center">
              <Loader2 size={32} className="mx-auto animate-spin text-primary" />
              <p className="mt-3 text-sm font-medium text-foreground">
                {runtimeStatus === 'checking'
                  ? 'Checking app status...'
                  : runtimePhase
                    ? PHASE_LABELS[runtimePhase] || runtimePhase
                    : 'Starting app...'}
                {runtimePhaseElapsed != null && runtimePhaseElapsed > 1 && (
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    ({Math.round(runtimePhaseElapsed)}s)
                  </span>
                )}
              </p>
              {runtimePhaseDetail && (
                <p className="mt-1 text-xs text-muted-foreground">
                  {runtimePhaseDetail}
                </p>
              )}
              {runtimePhase === 'installing' && (
                <p className="mt-3 text-xs text-muted-foreground/70">
                  This only happens once per published version — subsequent launches are instant.
                </p>
              )}
            </div>
          </div>
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-3">
            <AlertCircle size={40} className="text-destructive/40" />
            <p className="max-w-sm text-center text-sm text-muted-foreground">
              {runtimeError || 'Failed to start the app'}
            </p>
            <button
              onClick={() => {
                setRuntimeStatus('starting')
                setRuntimeError(null)
                apiClient.post<{ status: string; port?: number; error?: string }>(
                  `/apps/${app.id}/runtime/start`,
                  { source: `v${app.current_version}` }
                ).then((resp) => {
                  setRuntimeStatus(resp.status as any)
                  if (resp.port) setRuntimePort(resp.port)
                  if (resp.error) setRuntimeError(resp.error)
                }).catch(() => {
                  setRuntimeStatus('error')
                  setRuntimeError('Failed to start app')
                })
              }}
              className="flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-xs font-medium text-primary-foreground hover:bg-primary/90"
            >
              <RefreshCw size={12} />
              Retry
            </button>
          </div>
        )}

        {isFullscreen && runtimeStatus === 'running' && (
          <button
            onClick={() => setIsFullscreen(false)}
            className="absolute right-4 top-4 rounded-lg bg-card/90 p-2 text-muted-foreground shadow-lg backdrop-blur transition-colors hover:text-foreground"
            title="Exit fullscreen"
          >
            <Minimize2 size={16} />
          </button>
        )}
      </div>

      {/* Complete-setup modal */}
      {showSetupModal && setupWizard && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-2xl border border-border bg-card p-6">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-medium text-muted-foreground">Complete setup — {app.name}</span>
              <button
                onClick={() => setShowSetupModal(false)}
                className="rounded p-1 text-muted-foreground hover:text-foreground"
              >
                <X size={14} />
              </button>
            </div>
            <SetupWizardRenderer
              schema={setupWizard}
              onComplete={handleSetupComplete}
              onCancel={() => setShowSetupModal(false)}
            />
          </div>
        </div>
      )}
    </div>
  )
}
