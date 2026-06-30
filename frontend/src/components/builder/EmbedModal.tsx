import { useEffect, useState } from 'react'
import { X, Copy, Check, Loader2 } from 'lucide-react'
import { apiClient } from '@/api/client'

interface EmbedConfig {
  enabled: boolean
  allowed_origins: string[]
  embed_url: string
  snippet: string
}

export function EmbedModal({ appId, onClose }: { appId: string; onClose: () => void }) {
  const [cfg, setCfg] = useState<EmbedConfig | null>(null)
  const [enabled, setEnabled] = useState(false)
  const [originsText, setOriginsText] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    apiClient
      .get<EmbedConfig>(`/apps/${appId}/embed-config`)
      .then((c) => {
        setCfg(c)
        setEnabled(c.enabled)
        setOriginsText(c.allowed_origins.join('\n'))
      })
      .catch((e) => setError(e?.message || 'Failed to load'))
  }, [appId])

  const save = async () => {
    setSaving(true)
    setError(null)
    const origins = originsText
      .split(/[\n,]/)
      .map((o) => o.trim())
      .filter(Boolean)
    try {
      const c = await apiClient.put<EmbedConfig>(`/apps/${appId}/embed-config`, {
        enabled,
        allowed_origins: origins,
      })
      setCfg(c)
      setOriginsText(c.allowed_origins.join('\n'))
    } catch (e: any) {
      setError(e?.message || 'Failed to save')
    } finally {
      setSaving(false)
    }
  }

  const copySnippet = () => {
    if (!cfg?.snippet) return
    navigator.clipboard?.writeText(cfg.snippet)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="w-full max-w-lg overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h3 className="text-sm font-semibold">Embed this app</h3>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
            <X size={16} />
          </button>
        </div>

        <div className="space-y-4 p-4">
          {!cfg && !error && (
            <div className="flex justify-center py-8 text-muted-foreground">
              <Loader2 size={18} className="animate-spin" />
            </div>
          )}
          {error && <div className="rounded bg-red-950/40 px-3 py-2 text-xs text-red-300">{error}</div>}

          {cfg && (
            <>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                Allow this app to be embedded in external pages
              </label>

              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">
                  Allowed parent origins (one per line; blank = any)
                </label>
                <textarea
                  value={originsText}
                  onChange={(e) => setOriginsText(e.target.value)}
                  placeholder="https://portal.acme.com"
                  rows={3}
                  className="w-full resize-none rounded-lg border border-input bg-secondary px-3 py-2 font-mono text-xs"
                />
              </div>

              <button
                onClick={save}
                disabled={saving}
                className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
              >
                {saving ? 'Saving…' : 'Save'}
              </button>

              {cfg.enabled && cfg.snippet && (
                <div>
                  <label className="mb-1 block text-xs font-medium text-muted-foreground">
                    Embed snippet
                  </label>
                  <div className="relative">
                    <pre className="overflow-x-auto rounded-lg border border-border bg-zinc-950 p-3 pr-10 text-[11px] text-zinc-300">
                      {cfg.snippet}
                    </pre>
                    <button
                      onClick={copySnippet}
                      className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
                      title="Copy"
                    >
                      {copied ? <Check size={14} className="text-emerald-500" /> : <Copy size={14} />}
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
