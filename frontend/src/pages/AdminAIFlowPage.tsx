/**
 * Admin → AI Flow.
 *
 * A VISUAL map of how the platform builds an app — the pipeline stages in order,
 * with the actual system prompts that plug into each stage shown as chips. Click a
 * prompt to drill in: read its built-in default, see/edit the active override, and
 * understand exactly where it fits and what it impacts. Editing is gated behind a
 * clear warning and audited, because a bad prompt breaks generation platform-wide.
 */
import { useEffect, useState } from 'react'
import {
  Database, Sparkles, ShieldCheck, RefreshCw, Bug, ArrowRight, AlertTriangle,
  Loader2, RotateCcw, Save, X, Pencil, ChevronDown, ChevronRight, CheckCircle2,
} from 'lucide-react'
import { apiClient } from '@/api/client'

interface PromptRef { key: string; title: string }
interface Stage {
  id: string; title: string; icon: string; order: number; description: string
  inputs: string[]; outputs: string[]; prompts: PromptRef[]
}
interface PromptItem {
  key: string; title: string; description: string; stage: string
  default: string; override: string | null; is_overridden: boolean; effective: string
}

const ICONS: Record<string, React.ComponentType<{ size?: number; className?: string }>> = {
  database: Database, sparkles: Sparkles, 'shield-check': ShieldCheck,
  'refresh-cw': RefreshCw, bug: Bug,
}

export function AdminAIFlowPage() {
  const [stages, setStages] = useState<Stage[] | null>(null)
  const [prompts, setPrompts] = useState<Record<string, PromptItem>>({})
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)

  const load = async () => {
    setError(null)
    try {
      const [flow, plist] = await Promise.all([
        apiClient.get<{ stages: Stage[] }>('/admin/ai/flow'),
        apiClient.get<{ prompts: PromptItem[] }>('/admin/ai/prompts'),
      ])
      setStages(flow.stages)
      setPrompts(Object.fromEntries(plist.prompts.map((p) => [p.key, p])))
    } catch (e: any) {
      setError(e?.message || 'Failed to load AI flow')
    }
  }
  useEffect(() => { load() }, [])

  if (error) {
    return (
      <div className="p-8">
        <div className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertTriangle size={16} /> Couldn't load the AI flow. {error}
          <button onClick={load} className="ml-2 rounded bg-card px-2 py-1 text-xs hover:bg-accent">Retry</button>
        </div>
      </div>
    )
  }
  if (!stages) {
    return <div className="flex h-64 items-center justify-center"><Loader2 className="animate-spin text-primary" /></div>
  }

  const pipeline = stages.filter((s) => s.id !== 'bug_fix').sort((a, b) => a.order - b.order)
  const aside = stages.filter((s) => s.id === 'bug_fix')

  return (
    <div className="p-6 lg:p-8">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight">AI Build Flow</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
          How EveriApp turns a prompt into a running app. Each stage shows the system prompts that drive it —
          click any prompt to inspect or tune it. Changes apply platform-wide and are audited.
        </p>
      </header>

      {/* Main pipeline */}
      <div className="flex flex-wrap items-stretch gap-3">
        {pipeline.map((stage, i) => (
          <div key={stage.id} className="flex items-stretch gap-3">
            <StageCard stage={stage} prompts={prompts} onPick={setSelected} />
            {i < pipeline.length - 1 && (
              <div className="flex items-center self-center text-muted-foreground/50">
                <ArrowRight size={20} />
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Independent bug-fix path */}
      {aside.length > 0 && (
        <div className="mt-8">
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Independent path</p>
          <div className="flex flex-wrap gap-3">
            {aside.map((stage) => (
              <StageCard key={stage.id} stage={stage} prompts={prompts} onPick={setSelected} />
            ))}
          </div>
        </div>
      )}

      {selected && prompts[selected] && (
        <PromptEditor
          item={prompts[selected]}
          onClose={() => setSelected(null)}
          onSaved={async () => { await load() }}
        />
      )}
    </div>
  )
}

function StageCard({
  stage, prompts, onPick,
}: { stage: Stage; prompts: Record<string, PromptItem>; onPick: (k: string) => void }) {
  const Icon = ICONS[stage.icon] ?? Sparkles
  return (
    <div className="flex w-64 flex-col rounded-xl border border-border bg-card p-4 shadow-sm">
      <div className="mb-2 flex items-center gap-2">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <Icon size={16} />
        </div>
        <h3 className="text-sm font-semibold leading-tight">{stage.title}</h3>
      </div>
      <p className="mb-3 text-xs leading-snug text-muted-foreground">{stage.description}</p>

      <div className="mb-3 flex flex-wrap items-center gap-1 text-[10px] text-muted-foreground">
        <span className="rounded bg-muted px-1.5 py-0.5">{stage.inputs.join(', ')}</span>
        <ArrowRight size={10} />
        <span className="rounded bg-muted px-1.5 py-0.5">{stage.outputs.join(', ')}</span>
      </div>

      <div className="mt-auto space-y-1">
        {stage.prompts.length === 0 && (
          <span className="text-[10px] italic text-muted-foreground/60">no editable prompts</span>
        )}
        {stage.prompts.map((p) => {
          const item = prompts[p.key]
          return (
            <button
              key={p.key}
              onClick={() => onPick(p.key)}
              className="group flex w-full items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1.5 text-left text-[11px] hover:border-primary/40 hover:bg-primary/5"
            >
              <Pencil size={11} className="shrink-0 text-muted-foreground group-hover:text-primary" />
              <span className="min-w-0 flex-1 truncate">{p.title}</span>
              {item?.is_overridden && (
                <span className="shrink-0 rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-medium text-amber-600">
                  edited
                </span>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function PromptEditor({
  item, onClose, onSaved,
}: { item: PromptItem; onClose: () => void; onSaved: () => Promise<void> }) {
  const [text, setText] = useState(item.effective)
  const [saving, setSaving] = useState(false)
  const [showDefault, setShowDefault] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [savedOk, setSavedOk] = useState(false)

  useEffect(() => { setText(item.effective); setSavedOk(false); setErr(null) }, [item.key, item.effective])

  const dirty = text !== item.effective

  const save = async () => {
    if (!confirm(
      `Override the "${item.title}" prompt?\n\nThis changes how the AI builds apps for EVERYONE on this platform. ` +
      `A poorly-written prompt can break all app generation. The change is audited.\n\nContinue?`
    )) return
    setSaving(true); setErr(null)
    try {
      await apiClient.put(`/admin/ai/prompts/${item.key}`, { text })
      setSavedOk(true)
      await onSaved()
    } catch (e: any) {
      setErr(e?.message || 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const reset = async () => {
    if (!confirm(`Reset "${item.title}" back to the built-in default? Your override will be discarded.`)) return
    setSaving(true); setErr(null)
    try {
      await apiClient.post(`/admin/ai/prompts/${item.key}/reset`)
      setSavedOk(true)
      await onSaved()
    } catch (e: any) {
      setErr(e?.message || 'Reset failed')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onClick={onClose}>
      <div
        className="flex h-full w-full max-w-2xl flex-col border-l border-border bg-card shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between border-b border-border px-5 py-4">
          <div>
            <h2 className="text-lg font-semibold">{item.title}</h2>
            <p className="mt-0.5 text-xs text-muted-foreground">{item.description}</p>
            <code className="mt-1 inline-block rounded bg-muted px-1.5 py-0.5 text-[10px]">{item.key}</code>
            {item.is_overridden && (
              <span className="ml-2 rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-medium text-amber-600">
                overridden
              </span>
            )}
          </div>
          <button onClick={onClose} className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground">
            <X size={18} />
          </button>
        </div>

        <div className="flex items-start gap-2 border-b border-amber-500/20 bg-amber-500/5 px-5 py-3 text-xs text-amber-700">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            Editing this prompt changes how the AI builds apps <strong>platform-wide</strong>. A bad prompt can
            break all app generation. Every change is audited. Test with a throwaway app after saving.
          </span>
        </div>

        <div className="flex-1 overflow-auto p-5">
          <label className="mb-1 block text-xs font-medium text-muted-foreground">Active prompt text</label>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            spellCheck={false}
            className="h-80 w-full resize-y rounded-lg border border-border bg-background p-3 font-mono text-xs leading-relaxed focus:border-primary focus:outline-none"
          />

          <button
            onClick={() => setShowDefault((v) => !v)}
            className="mt-3 flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            {showDefault ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            View built-in default
          </button>
          {showDefault && (
            <pre className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-muted/40 p-3 font-mono text-[11px] text-muted-foreground">
              {item.default}
            </pre>
          )}

          {err && (
            <div className="mt-3 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              {err}
            </div>
          )}
          {savedOk && !dirty && (
            <div className="mt-3 flex items-center gap-1.5 text-xs text-success">
              <CheckCircle2 size={14} /> Saved.
            </div>
          )}
        </div>

        <div className="flex items-center justify-between border-t border-border px-5 py-3">
          <button
            onClick={reset}
            disabled={saving || !item.is_overridden}
            className="flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-40"
            title={item.is_overridden ? 'Discard override, restore default' : 'No override to reset'}
          >
            <RotateCcw size={14} /> Reset to default
          </button>
          <div className="flex items-center gap-2">
            <button onClick={onClose} className="rounded-lg px-3 py-2 text-xs text-muted-foreground hover:bg-accent">
              Cancel
            </button>
            <button
              onClick={save}
              disabled={saving || !dirty}
              className="flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
              Save override
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
