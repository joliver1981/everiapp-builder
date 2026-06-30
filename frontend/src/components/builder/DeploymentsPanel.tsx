import { useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  CircleSlash,
  ExternalLink,
  FileText,
  Loader2,
  RefreshCw,
  Rocket,
  Square,
} from 'lucide-react'

import { useDeploymentsStore } from '@/stores/deploymentsStore'
import { cn } from '@/lib/utils'

import { DeploymentLogsModal } from './DeploymentLogsModal'

interface Props {
  appId: string
  currentVersion: number
}

const STATUS_STYLES: Record<string, { dot: string; label: string }> = {
  pending:   { dot: 'bg-muted-foreground',          label: 'pending' },
  building:  { dot: 'bg-warning animate-pulse',     label: 'building' },
  uploading: { dot: 'bg-warning animate-pulse',     label: 'uploading' },
  running:   { dot: 'bg-success animate-pulse',     label: 'running' },
  stopped:   { dot: 'bg-muted',                     label: 'stopped' },
  failed:    { dot: 'bg-destructive',               label: 'failed' },
}

export function DeploymentsPanel({ appId, currentVersion }: Props) {
  const {
    targets,
    fetchTargets,
    deploymentsByApp,
    isLoadingDeployments,
    fetchDeployments,
    deployVersion,
    stopDeployment,
    redeploy,
  } = useDeploymentsStore()

  const deployments = deploymentsByApp[appId] || []
  const isLoading = isLoadingDeployments[appId] || false

  const [version, setVersion] = useState<number>(currentVersion)
  const [targetId, setTargetId] = useState<string>('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [logDeployId, setLogDeployId] = useState<string | null>(null)

  useEffect(() => {
    fetchTargets()
    fetchDeployments(appId)
  }, [appId, fetchTargets, fetchDeployments])

  // Refresh deployments while anything is mid-flight, so we see status transitions.
  useEffect(() => {
    const inFlight = deployments.some((d) =>
      ['pending', 'building', 'uploading'].includes(d.status),
    )
    if (!inFlight) return
    const id = setInterval(() => fetchDeployments(appId), 3000)
    return () => clearInterval(id)
  }, [deployments, appId, fetchDeployments])

  useEffect(() => {
    setVersion(currentVersion)
  }, [currentVersion])

  const activeTargets = useMemo(() => targets.filter((t) => t.is_active), [targets])

  const handleDeploy = async () => {
    if (!targetId) {
      setError('Pick a target first')
      return
    }
    if (version <= 0) {
      setError('Save a version before deploying')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await deployVersion(appId, version, targetId)
    } catch (e: any) {
      setError(e?.message || 'Deploy failed')
    } finally {
      setBusy(false)
    }
  }

  const targetName = (id: string) => targets.find((t) => t.id === id)?.name || id.slice(0, 8)
  const targetEnv = (id: string) => targets.find((t) => t.id === id)?.environment

  return (
    <>
      <div className="flex h-full flex-col">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h3 className="text-sm font-semibold">Deployments</h3>
          <button
            onClick={() => fetchDeployments(appId)}
            className="rounded-lg p-1 text-muted-foreground hover:text-foreground"
            title="Refresh"
          >
            <RefreshCw size={14} className={cn(isLoading && 'animate-spin')} />
          </button>
        </div>

        {/* Deploy form */}
        <div className="border-b border-border bg-muted/40 p-4">
          <div className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="mb-1 block text-[10px] font-medium uppercase text-muted-foreground">Version</label>
                <input
                  type="number"
                  min={1}
                  max={Math.max(currentVersion, 1)}
                  value={version}
                  onChange={(e) => setVersion(Number(e.target.value))}
                  className="w-full rounded-lg border border-input bg-background px-2 py-1.5 text-xs"
                />
              </div>
              <div>
                <label className="mb-1 block text-[10px] font-medium uppercase text-muted-foreground">Target</label>
                <select
                  value={targetId}
                  onChange={(e) => setTargetId(e.target.value)}
                  className="w-full rounded-lg border border-input bg-background px-2 py-1.5 text-xs"
                >
                  <option value="">— pick —</option>
                  {activeTargets.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name} ({t.environment})
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <button
              onClick={handleDeploy}
              disabled={busy || !targetId || version <= 0}
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {busy ? <Loader2 size={12} className="animate-spin" /> : <Rocket size={12} />}
              Deploy v{version}
            </button>
            {error && (
              <div className="flex items-start gap-1.5 rounded-lg bg-destructive/10 px-2 py-1.5 text-[11px] text-destructive">
                <AlertCircle size={12} className="mt-0.5 shrink-0" />
                {error}
              </div>
            )}
            {activeTargets.length === 0 && (
              <p className="text-[11px] text-muted-foreground">
                No active targets. Add one in <span className="font-medium">Admin → Deployment Targets</span>.
              </p>
            )}
          </div>
        </div>

        {/* History */}
        <div className="flex-1 overflow-auto">
          {isLoading && deployments.length === 0 ? (
            <div className="flex justify-center py-8">
              <Loader2 size={20} className="animate-spin text-muted-foreground" />
            </div>
          ) : deployments.length === 0 ? (
            <div className="px-4 py-8 text-center">
              <CircleSlash size={28} className="mx-auto text-muted-foreground/30" />
              <p className="mt-2 text-xs text-muted-foreground">No deployments yet</p>
            </div>
          ) : (
            <div className="space-y-2 p-3">
              {deployments.map((d) => {
                const style = STATUS_STYLES[d.status] || STATUS_STYLES.pending
                return (
                  <div key={d.id} className="rounded-lg border border-border bg-card p-3">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="flex items-center gap-1.5">
                          <span className={cn('h-2 w-2 rounded-full', style.dot)} />
                          <span className="text-xs font-semibold">v{d.version}</span>
                          <span className="text-[10px] text-muted-foreground">→</span>
                          <span className="text-xs">{targetName(d.target_id)}</span>
                          {targetEnv(d.target_id) && (
                            <span className="rounded bg-muted px-1 py-0.5 text-[9px] text-muted-foreground">
                              {targetEnv(d.target_id)}
                            </span>
                          )}
                        </div>
                        <div className="mt-1 text-[10px] text-muted-foreground">
                          {style.label}
                          {d.allocated_port ? ` · port ${d.allocated_port}` : ''}
                          {d.last_health_status === 'error' && (
                            <span className="ml-1 text-destructive">· unhealthy</span>
                          )}
                          {d.last_health_status === 'ok' && (
                            <span className="ml-1 text-success">· healthy</span>
                          )}
                        </div>
                        {d.public_url && d.status === 'running' && (
                          <a
                            href={d.public_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="mt-1 flex items-center gap-1 truncate text-[11px] text-primary hover:underline"
                          >
                            <ExternalLink size={10} />
                            {d.public_url}
                          </a>
                        )}
                        {d.error && (
                          <div className="mt-1 truncate rounded bg-destructive/10 px-1.5 py-1 text-[10px] text-destructive" title={d.error}>
                            {d.error}
                          </div>
                        )}
                      </div>
                      <div className="flex shrink-0 flex-col gap-1">
                        <button
                          onClick={() => setLogDeployId(d.id)}
                          className="rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
                          title="View logs"
                        >
                          <FileText size={12} />
                        </button>
                        {['running', 'pending', 'building', 'uploading'].includes(d.status) && (
                          <button
                            onClick={async () => { await stopDeployment(d.id, appId) }}
                            className="rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-destructive"
                            title="Stop"
                          >
                            <Square size={12} />
                          </button>
                        )}
                        {['stopped', 'failed'].includes(d.status) && (
                          <button
                            onClick={async () => { await redeploy(d.id, appId) }}
                            className="rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
                            title="Redeploy"
                          >
                            <CheckCircle2 size={12} />
                          </button>
                        )}
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>

      {logDeployId && (
        <DeploymentLogsModal
          deploymentId={logDeployId}
          onClose={() => setLogDeployId(null)}
        />
      )}
    </>
  )
}
