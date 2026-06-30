/**
 * SetupWizardPreview — Builder right-panel for authoring an app's setup wizard.
 *
 * Three modes:
 *  - Builder: structured form editor (steps + fields, all types, reorder)
 *  - Preview: live SetupWizardRenderer; completing it actually applies the
 *    values to this app via POST /apps/{id}/setup
 *  - JSON: raw schema for power users
 */
import { useEffect, useState } from 'react'
import {
  Wand2, Loader2, Eye, Code2, Trash2, Plus, X,
  ChevronUp, ChevronDown, SlidersHorizontal, Check,
} from 'lucide-react'
import { apiClient } from '@/api/client'
import { SetupWizardRenderer, type WizardSchema, type WizardField, type WizardStep } from './SetupWizardRenderer'
import { cn } from '@/lib/utils'

const FIELD_TYPES: WizardField['type'][] = [
  'string', 'secret', 'number', 'boolean', 'select', 'url', 'connection', 'global_secret',
]

const FIELD_TYPE_LABELS: Record<string, string> = {
  string: 'Text',
  secret: 'Secret (encrypted)',
  number: 'Number',
  boolean: 'Toggle',
  select: 'Dropdown',
  url: 'URL',
  connection: 'Database connection',
  global_secret: 'Global secret (pointer)',
}

interface SetupWizardPreviewProps {
  appId: string
}

export function SetupWizardPreview({ appId }: SetupWizardPreviewProps) {
  const [wizard, setWizard] = useState<WizardSchema | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [mode, setMode] = useState<'builder' | 'preview' | 'json'>('builder')
  const [jsonText, setJsonText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [savedMsg, setSavedMsg] = useState<string | null>(null)

  useEffect(() => {
    loadWizard()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appId])

  const loadWizard = async () => {
    setIsLoading(true)
    try {
      const data = await apiClient.get<WizardSchema | Record<string, never>>(`/apps/${appId}/wizard`)
      if (data && 'steps' in data && data.steps) {
        setWizard(data as WizardSchema)
        setJsonText(JSON.stringify(data, null, 2))
      } else {
        setWizard(null)
        setJsonText('')
      }
    } catch {
      setWizard(null)
    } finally {
      setIsLoading(false)
    }
  }

  const saveWizard = async (schema: WizardSchema | null) => {
    setIsSaving(true)
    setError(null)
    setSavedMsg(null)
    try {
      await apiClient.put(`/apps/${appId}/wizard`, schema || {})
      setWizard(schema)
      if (schema) setJsonText(JSON.stringify(schema, null, 2))
      setSavedMsg('Saved')
      setTimeout(() => setSavedMsg(null), 2000)
    } catch (err: any) {
      let detail = err?.message || 'Save failed'
      try {
        const parsed = JSON.parse(err.message)
        if (typeof parsed.detail === 'string') detail = parsed.detail
      } catch { /* not JSON */ }
      setError(detail)
    } finally {
      setIsSaving(false)
    }
  }

  // ---- immutable schema editing helpers -----------------------------------
  const update = (fn: (w: WizardSchema) => WizardSchema) => {
    setWizard((prev) => (prev ? fn(structuredClone(prev)) : prev))
  }

  const updateStep = (si: number, patch: Partial<WizardStep>) =>
    update((w) => { w.steps[si] = { ...w.steps[si], ...patch }; return w })

  const updateField = (si: number, fi: number, patch: Partial<WizardField>) =>
    update((w) => {
      w.steps[si].fields[fi] = { ...w.steps[si].fields[fi], ...patch }
      return w
    })

  const move = <T,>(arr: T[], from: number, to: number) => {
    if (to < 0 || to >= arr.length) return
    const [item] = arr.splice(from, 1)
    arr.splice(to, 0, item)
  }

  const handleJsonSave = () => {
    try {
      const parsed = JSON.parse(jsonText)
      saveWizard(parsed)
    } catch {
      setError('Invalid JSON — fix the syntax and try again.')
    }
  }

  const handleDeleteWizard = () => {
    saveWizard(null)
    setWizard(null)
    setJsonText('')
  }

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loader2 size={20} className="animate-spin text-muted-foreground" />
      </div>
    )
  }

  if (!wizard) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-6">
        <Wand2 size={40} className="text-muted-foreground/30" />
        <div className="text-center">
          <p className="text-sm font-medium">No Setup Wizard</p>
          <p className="mt-1 text-xs text-muted-foreground">
            Define what users must configure when they install this app —
            credentials, connections, options. Ask the AI to &quot;create a setup
            wizard&quot;, or build one by hand.
          </p>
        </div>
        <button
          onClick={() => {
            const defaultSchema: WizardSchema = {
              title: 'App Setup',
              description: 'Configure your app settings',
              steps: [{
                title: 'Configuration',
                fields: [{
                  key: 'example_field',
                  label: 'Example Field',
                  type: 'string',
                  description: 'Replace this with your own fields',
                }],
              }],
            }
            saveWizard(defaultSchema)
          }}
          className="flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground hover:bg-primary/90"
        >
          <Plus size={12} />
          Create Wizard
        </button>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <Wand2 size={14} className="text-primary" />
          <span className="text-sm font-medium">Setup Wizard</span>
          {savedMsg && (
            <span className="flex items-center gap-1 text-xs text-success">
              <Check size={10} /> {savedMsg}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          <div className="flex rounded-lg bg-muted p-0.5">
            <button
              onClick={() => setMode('builder')}
              title="Form builder"
              className={cn(
                'rounded-md px-2 py-1 text-xs',
                mode === 'builder' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground'
              )}
            >
              <SlidersHorizontal size={12} />
            </button>
            <button
              onClick={() => setMode('preview')}
              title="Preview (completing it applies values to this app)"
              className={cn(
                'rounded-md px-2 py-1 text-xs',
                mode === 'preview' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground'
              )}
            >
              <Eye size={12} />
            </button>
            <button
              onClick={() => setMode('json')}
              title="Raw JSON"
              className={cn(
                'rounded-md px-2 py-1 text-xs',
                mode === 'json' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground'
              )}
            >
              <Code2 size={12} />
            </button>
          </div>
          <button
            onClick={handleDeleteWizard}
            className="rounded-lg p-1.5 text-muted-foreground hover:text-destructive"
            title="Delete wizard"
          >
            <Trash2 size={12} />
          </button>
        </div>
      </div>

      {error && (
        <div className="mx-4 mt-3 rounded-lg bg-red-500/10 p-2 text-xs text-red-700 dark:text-red-300">
          {error}
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4">
        {mode === 'preview' ? (
          <SetupWizardRenderer
            schema={wizard}
            onComplete={async (values) => {
              try {
                const res = await apiClient.post<{ applied: number; complete: boolean }>(
                  `/apps/${appId}/setup`, { values },
                )
                setSavedMsg(`Applied ${res.applied} setting(s)`)
                setTimeout(() => setSavedMsg(null), 3000)
              } catch (err: any) {
                setError(err?.message || 'Failed to apply values')
              }
            }}
          />
        ) : mode === 'json' ? (
          <div className="flex flex-col gap-2">
            <textarea
              value={jsonText}
              onChange={(e) => setJsonText(e.target.value)}
              className="h-80 rounded-lg border border-border bg-background p-3 font-mono text-xs outline-none focus:ring-1 focus:ring-primary"
              spellCheck={false}
            />
            <button
              onClick={handleJsonSave}
              disabled={isSaving}
              className="flex items-center justify-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {isSaving ? <Loader2 size={12} className="animate-spin" /> : null}
              Save JSON
            </button>
          </div>
        ) : (
          /* ---- Builder mode ---- */
          <div className="flex flex-col gap-4">
            {/* Wizard meta */}
            <div className="flex flex-col gap-2 rounded-lg border border-border p-3">
              <input
                value={wizard.title}
                onChange={(e) => update((w) => { w.title = e.target.value; return w })}
                placeholder="Wizard title"
                className="rounded-lg border border-input bg-secondary px-2.5 py-1.5 text-sm font-medium focus:outline-none focus:ring-1 focus:ring-ring"
              />
              <input
                value={wizard.description || ''}
                onChange={(e) => update((w) => { w.description = e.target.value; return w })}
                placeholder="Short intro shown at the top of the wizard"
                className="rounded-lg border border-input bg-secondary px-2.5 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
              />
            </div>

            {/* Steps */}
            {wizard.steps.map((step, si) => (
              <div key={si} className="rounded-lg border border-border">
                <div className="flex items-center gap-1.5 border-b border-border px-3 py-2">
                  <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                    Step {si + 1}
                  </span>
                  <input
                    value={step.title}
                    onChange={(e) => updateStep(si, { title: e.target.value })}
                    placeholder="Step title"
                    className="flex-1 bg-transparent text-sm font-medium focus:outline-none"
                  />
                  <button
                    onClick={() => update((w) => { move(w.steps, si, si - 1); return w })}
                    disabled={si === 0}
                    className="rounded p-1 text-muted-foreground hover:text-foreground disabled:opacity-30"
                    title="Move step up"
                  >
                    <ChevronUp size={12} />
                  </button>
                  <button
                    onClick={() => update((w) => { move(w.steps, si, si + 1); return w })}
                    disabled={si === wizard.steps.length - 1}
                    className="rounded p-1 text-muted-foreground hover:text-foreground disabled:opacity-30"
                    title="Move step down"
                  >
                    <ChevronDown size={12} />
                  </button>
                  <button
                    onClick={() => update((w) => { w.steps.splice(si, 1); return w })}
                    className="rounded p-1 text-muted-foreground hover:text-destructive"
                    title="Remove step"
                  >
                    <X size={12} />
                  </button>
                </div>

                <div className="flex flex-col gap-3 p-3">
                  {step.fields.map((field, fi) => (
                    <div key={fi} className="rounded-lg border border-border/60 bg-secondary/40 p-2.5">
                      <div className="flex items-center gap-1.5">
                        <input
                          value={field.label}
                          onChange={(e) => updateField(si, fi, { label: e.target.value })}
                          placeholder="Label"
                          className="flex-1 rounded border border-input bg-background px-2 py-1 text-xs font-medium focus:outline-none focus:ring-1 focus:ring-ring"
                        />
                        <select
                          value={field.type}
                          onChange={(e) => updateField(si, fi, { type: e.target.value as WizardField['type'] })}
                          className="rounded border border-input bg-background px-1.5 py-1 text-xs focus:outline-none"
                        >
                          {FIELD_TYPES.map((t) => (
                            <option key={t} value={t}>{FIELD_TYPE_LABELS[t]}</option>
                          ))}
                        </select>
                        <button
                          onClick={() => update((w) => { move(w.steps[si].fields, fi, fi - 1); return w })}
                          disabled={fi === 0}
                          className="rounded p-0.5 text-muted-foreground hover:text-foreground disabled:opacity-30"
                        >
                          <ChevronUp size={11} />
                        </button>
                        <button
                          onClick={() => update((w) => { move(w.steps[si].fields, fi, fi + 1); return w })}
                          disabled={fi === step.fields.length - 1}
                          className="rounded p-0.5 text-muted-foreground hover:text-foreground disabled:opacity-30"
                        >
                          <ChevronDown size={11} />
                        </button>
                        <button
                          onClick={() => update((w) => { w.steps[si].fields.splice(fi, 1); return w })}
                          className="rounded p-0.5 text-muted-foreground hover:text-destructive"
                        >
                          <X size={11} />
                        </button>
                      </div>

                      <div className="mt-2 grid grid-cols-2 gap-1.5">
                        <input
                          value={field.key}
                          onChange={(e) => updateField(si, fi, { key: e.target.value })}
                          placeholder="key (snake_case)"
                          className="rounded border border-input bg-background px-2 py-1 font-mono text-[11px] focus:outline-none focus:ring-1 focus:ring-ring"
                        />
                        <input
                          value={field.placeholder || ''}
                          onChange={(e) => updateField(si, fi, { placeholder: e.target.value })}
                          placeholder="placeholder"
                          className="rounded border border-input bg-background px-2 py-1 text-[11px] focus:outline-none focus:ring-1 focus:ring-ring"
                        />
                      </div>
                      <input
                        value={field.description || ''}
                        onChange={(e) => updateField(si, fi, { description: e.target.value })}
                        placeholder="Help text shown under the label"
                        className="mt-1.5 w-full rounded border border-input bg-background px-2 py-1 text-[11px] focus:outline-none focus:ring-1 focus:ring-ring"
                      />

                      {field.type === 'select' && (
                        <input
                          value={(field.options || []).join(', ')}
                          onChange={(e) => updateField(si, fi, {
                            options: e.target.value.split(',').map((o) => o.trim()).filter(Boolean),
                          })}
                          placeholder="Options, comma-separated"
                          className="mt-1.5 w-full rounded border border-input bg-background px-2 py-1 text-[11px] focus:outline-none focus:ring-1 focus:ring-ring"
                        />
                      )}
                      {field.type === 'connection' && (
                        <input
                          value={field.dialect || ''}
                          onChange={(e) => updateField(si, fi, { dialect: e.target.value.trim() })}
                          placeholder="Restrict to dialect (optional): postgres, mysql, mssql, oracle, sqlite"
                          className="mt-1.5 w-full rounded border border-input bg-background px-2 py-1 text-[11px] focus:outline-none focus:ring-1 focus:ring-ring"
                        />
                      )}

                      <label className="mt-1.5 flex items-center gap-1.5 text-[11px] text-muted-foreground">
                        <input
                          type="checkbox"
                          checked={!!field.required}
                          onChange={(e) => updateField(si, fi, { required: e.target.checked })}
                          className="rounded border-input"
                        />
                        Required — installs prompt until this is filled
                      </label>
                    </div>
                  ))}

                  <button
                    onClick={() => update((w) => {
                      w.steps[si].fields.push({
                        key: `field_${w.steps[si].fields.length + 1}`,
                        label: 'New field',
                        type: 'string',
                      })
                      return w
                    })}
                    className="flex items-center justify-center gap-1 rounded-lg border border-dashed border-border py-1.5 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
                  >
                    <Plus size={11} /> Add field
                  </button>
                </div>
              </div>
            ))}

            <button
              onClick={() => update((w) => {
                w.steps.push({ title: `Step ${w.steps.length + 1}`, fields: [] })
                return w
              })}
              className="flex items-center justify-center gap-1 rounded-lg border border-dashed border-border py-2 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
            >
              <Plus size={12} /> Add step
            </button>

            <button
              onClick={() => saveWizard(wizard)}
              disabled={isSaving}
              className="flex items-center justify-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {isSaving ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
              Save Wizard
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
