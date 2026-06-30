import { useEffect, useRef, useState } from 'react'
import { Loader2, RefreshCw, X } from 'lucide-react'

import { useDeploymentsStore } from '@/stores/deploymentsStore'

interface Props {
  deploymentId: string
  onClose: () => void
}

export function DeploymentLogsModal({ deploymentId, onClose }: Props) {
  const { fetchLogs } = useDeploymentsStore()
  const [lines, setLines] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const scrollRef = useRef<HTMLDivElement>(null)

  const refresh = async () => {
    setLoading(true)
    try {
      const result = await fetchLogs(deploymentId, 500)
      setLines(result)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
  }, [deploymentId])

  useEffect(() => {
    if (!autoRefresh) return
    const id = setInterval(refresh, 4000)
    return () => clearInterval(id)
  }, [autoRefresh, deploymentId])

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [lines])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="flex h-[80vh] w-full max-w-4xl flex-col rounded-2xl border border-border bg-card">
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <h2 className="text-sm font-semibold">Deployment logs</h2>
          <div className="flex items-center gap-2">
            <label className="flex items-center gap-1 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
                className="h-3 w-3"
              />
              Auto-refresh
            </label>
            <button
              onClick={refresh}
              disabled={loading}
              className="flex items-center gap-1 rounded-lg bg-muted px-2 py-1 text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
            >
              {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
              Refresh
            </button>
            <button
              onClick={onClose}
              className="rounded-lg p-1 text-muted-foreground hover:text-foreground"
            >
              <X size={16} />
            </button>
          </div>
        </div>
        <div
          ref={scrollRef}
          className="flex-1 overflow-auto bg-black/90 p-4 font-mono text-xs text-green-200"
        >
          {lines.length === 0 ? (
            <div className="text-muted-foreground">— no log output —</div>
          ) : (
            lines.map((line, i) => (
              <div key={i} className="whitespace-pre-wrap break-all">
                {line}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}
