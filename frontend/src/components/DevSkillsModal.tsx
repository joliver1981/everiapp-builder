/**
 * DevSkillsModal — this developer's personal "skills": standing preferences
 * the AI applies to every app THEY build (e.g. "when using SQLite, enable WAL
 * for concurrent writes"). Org-wide standards that apply to every developer
 * live in Admin → Platform → Organization Conventions.
 */
import { useEffect, useState } from 'react'
import { BookOpen, Loader2, X } from 'lucide-react'
import { apiClient } from '@/api/client'
import { useAuthStore } from '@/stores/authStore'

export function DevSkillsModal({ onClose }: { onClose: () => void }) {
  const user = useAuthStore((s) => s.user)
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    apiClient.get<{ dev_standards: string }>('/auth/me/dev-standards')
      .then((r) => setText(r.dev_standards))
      .catch((e: any) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      await apiClient.put('/auth/me/dev-standards', { dev_standards: text })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="flex max-h-[80vh] w-[640px] flex-col rounded-xl border border-border bg-card shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <div className="flex items-center gap-2">
            <BookOpen size={16} className="text-primary" />
            <h3 className="text-sm font-semibold">My developer skills</h3>
          </div>
          <button onClick={onClose} className="rounded p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground">
            <X size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-5">
          <p className="mb-3 text-xs text-muted-foreground">
            Standing preferences the AI applies to <span className="font-medium text-foreground">every app you build</span> —
            defaults, conventions, and approaches you always want followed. Written in plain language, one per line works well.
          </p>
          {loading ? (
            <div className="flex justify-center py-8"><Loader2 size={20} className="animate-spin text-muted-foreground" /></div>
          ) : (
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={12}
              maxLength={40000}
              placeholder={'Examples:\n- When using the app\'s SQLite store, optimize for concurrent writes (WAL) by default.\n- Every list view gets a loading skeleton and an empty state.\n- Prefer aiDecide over regex for any fuzzy matching.\n- Use my company\'s date format: YYYY-MM-DD.'}
              className="w-full rounded-lg border border-input bg-secondary p-3 font-mono text-xs leading-5 focus:outline-none focus:ring-2 focus:ring-ring"
            />
          )}
          {!loading && (
            <p className="mt-1 text-right text-[11px] text-muted-foreground">
              {text.length.toLocaleString()} / 40,000
            </p>
          )}
          {error && <p className="mt-2 text-xs text-destructive">{error}</p>}
          <p className="mt-3 text-[11px] text-muted-foreground">
            {user?.role === 'admin'
              ? 'Org-wide standards that apply to every developer live in Platform → Organization Conventions.'
              : 'Admins can set org-wide standards for all developers under Platform → Organization Conventions.'}
          </p>
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-3">
          {saved && <span className="text-xs text-success">Saved — applies to your next generation turn</span>}
          <button onClick={onClose} className="rounded-lg px-4 py-2 text-sm text-muted-foreground hover:text-foreground">
            Close
          </button>
          <button
            onClick={save}
            disabled={saving || loading}
            className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {saving && <Loader2 size={14} className="animate-spin" />}
            Save
          </button>
        </div>
      </div>
    </div>
  )
}
