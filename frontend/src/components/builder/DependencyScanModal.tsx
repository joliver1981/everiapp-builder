import { useEffect, useState } from 'react'
import { X, Loader2, PackageCheck } from 'lucide-react'
import { apiClient } from '@/api/client'
import { cn } from '@/lib/utils'

interface DepFinding {
  package: string
  current: string
  severity: 'high' | 'medium' | 'low'
  issue: string
  recommendation: string
}
interface DepReport {
  package_json_found: boolean
  finding_count: number
  counts: Record<string, number>
  findings: DepFinding[]
}

const SEV = {
  high: 'bg-red-500/10 text-red-400',
  medium: 'bg-amber-500/10 text-amber-400',
  low: 'bg-sky-500/10 text-sky-400',
}

export function DependencyScanModal({ appId, onClose }: { appId: string; onClose: () => void }) {
  const [report, setReport] = useState<DepReport | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    apiClient.get<DepReport>(`/apps/${appId}/dependency-scan`).then(setReport).catch((e) => setError(e?.message))
  }, [appId])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div className="flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
           onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h3 className="flex items-center gap-2 text-sm font-semibold"><PackageCheck size={16} className="text-primary" /> Dependency check</h3>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground"><X size={16} /></button>
        </div>
        <div className="flex-1 overflow-y-auto p-4">
          {error && <p className="text-sm text-red-400">{error}</p>}
          {!report && !error && <div className="flex justify-center py-8"><Loader2 className="animate-spin text-muted-foreground" /></div>}
          {report && !report.package_json_found && <p className="text-sm text-muted-foreground">No package.json found in the draft.</p>}
          {report && report.package_json_found && report.findings.length === 0 && (
            <p className="text-sm text-green-400">No dependency issues found. 🎉</p>
          )}
          {report && report.findings.length > 0 && (
            <ul className="space-y-2">
              {report.findings.map((f, i) => (
                <li key={i} className="rounded-lg border border-border p-3">
                  <div className="flex items-center gap-2">
                    <span className={cn('rounded px-1.5 py-0.5 text-[10px] uppercase', SEV[f.severity])}>{f.severity}</span>
                    <span className="font-mono text-sm">{f.package}</span>
                    <span className="text-xs text-muted-foreground">{f.current}</span>
                  </div>
                  <p className="mt-1 text-xs">{f.issue}</p>
                  <p className="mt-0.5 text-xs text-muted-foreground">→ {f.recommendation}</p>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  )
}
