import { useEffect, useState } from 'react'
import { Loader2, RotateCcw } from 'lucide-react'
import { apiClient } from '@/api/client'

interface Analytics {
  app_id: string
  days: number
  total_events: number
  unique_users: number
  by_type: Record<string, number>
  by_day: { day: string; count: number }[]
  llm_cost_usd: number
}

export function AppAnalyticsPanel({ appId }: { appId: string }) {
  const [data, setData] = useState<Analytics | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = () => {
    setLoading(true)
    setError(null)
    apiClient
      .get<Analytics>(`/admin/apps/${appId}/analytics?days=30`)
      .then(setData)
      .catch((e) => setError(e?.message || 'Failed to load analytics'))
      .finally(() => setLoading(false))
  }

  useEffect(load, [appId])

  const maxDay = data ? Math.max(1, ...data.by_day.map((d) => d.count)) : 1

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h3 className="text-sm font-semibold">Usage · last 30 days</h3>
        <button onClick={load} className="text-muted-foreground hover:text-foreground">
          <RotateCcw size={14} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {loading && (
          <div className="flex justify-center py-10 text-muted-foreground">
            <Loader2 size={18} className="animate-spin" />
          </div>
        )}
        {error && <div className="py-6 text-center text-xs text-red-400">{error}</div>}
        {data && !loading && (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-2">
              <Stat label="Total events" value={data.total_events} />
              <Stat label="Unique users" value={data.unique_users} />
              <Stat label="LLM cost" value={`$${data.llm_cost_usd.toFixed(2)}`} />
              <Stat label="Event types" value={Object.keys(data.by_type).length} />
            </div>

            {Object.keys(data.by_type).length > 0 && (
              <div>
                <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground/70">
                  By type
                </p>
                <div className="space-y-1">
                  {Object.entries(data.by_type).map(([type, count]) => (
                    <div key={type} className="flex items-center justify-between text-xs">
                      <span className="text-muted-foreground">{type}</span>
                      <span className="font-medium">{count}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {data.by_day.length > 0 && (
              <div>
                <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground/70">
                  Activity
                </p>
                <div className="flex h-20 items-end gap-0.5">
                  {data.by_day.map((d) => (
                    <div
                      key={d.day}
                      className="flex-1 rounded-sm bg-primary/60"
                      style={{ height: `${Math.max(4, (d.count / maxDay) * 100)}%` }}
                      title={`${d.day}: ${d.count}`}
                    />
                  ))}
                </div>
              </div>
            )}

            {data.total_events === 0 && (
              <p className="py-6 text-center text-xs text-muted-foreground">
                No usage recorded yet. Events appear once the app is launched.
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border border-border p-3">
      <p className="text-lg font-semibold">{value}</p>
      <p className="text-[11px] text-muted-foreground">{label}</p>
    </div>
  )
}
