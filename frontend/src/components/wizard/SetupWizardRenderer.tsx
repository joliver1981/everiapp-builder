/**
 * SetupWizardRenderer — Renders a multi-step setup wizard from a JSON schema.
 * Used in the builder preview, marketplace install, and post-install setup.
 */
import { useEffect, useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight, Check, Loader2, PlugZap } from 'lucide-react'
import { apiClient } from '@/api/client'
import { cn } from '@/lib/utils'

export interface WizardField {
  key: string
  label: string
  type: 'string' | 'secret' | 'number' | 'boolean' | 'select' | 'url' | 'connection' | 'global_secret'
  description?: string
  required?: boolean
  placeholder?: string
  default?: string | number | boolean
  options?: string[]
  /** For type=connection: only offer connections of this SQL dialect (e.g. "postgres"). */
  dialect?: string
}

export interface WizardStep {
  title: string
  description?: string
  fields: WizardField[]
}

export interface WizardSchema {
  title: string
  description?: string
  steps: WizardStep[]
}

interface PickableConnection {
  id: string
  name: string
  description: string
  kind: string
  dialect: string
}

interface PickableSecret {
  id: string
  name: string
  category: string
  is_set: boolean
}

interface SetupWizardRendererProps {
  schema: WizardSchema
  onComplete: (values: Record<string, string | number | boolean>) => void
  onCancel?: () => void
  initialValues?: Record<string, string | number | boolean>
}

export function SetupWizardRenderer({ schema, onComplete, onCancel, initialValues = {} }: SetupWizardRendererProps) {
  const [currentStep, setCurrentStep] = useState(0)
  const [values, setValues] = useState<Record<string, string | number | boolean>>(
    () => {
      const defaults: Record<string, string | number | boolean> = {}
      for (const step of schema.steps) {
        for (const field of step.fields) {
          if (field.default !== undefined) defaults[field.key] = field.default
        }
      }
      return { ...defaults, ...initialValues }
    }
  )

  // Pickable lists — fetched once, only when the schema actually uses the type.
  const needsConnections = useMemo(
    () => schema.steps.some((s) => s.fields.some((f) => f.type === 'connection')),
    [schema],
  )
  const needsSecrets = useMemo(
    () => schema.steps.some((s) => s.fields.some((f) => f.type === 'global_secret')),
    [schema],
  )
  const [connections, setConnections] = useState<PickableConnection[] | null>(null)
  const [globalSecrets, setGlobalSecrets] = useState<PickableSecret[] | null>(null)
  // Per-connection test state: key -> {state, message}
  const [connTests, setConnTests] = useState<Record<string, { state: 'testing' | 'ok' | 'fail'; message: string }>>({})

  useEffect(() => {
    if (needsConnections && connections === null) {
      apiClient.get<PickableConnection[]>('/admin/connections/pickable')
        .then(setConnections)
        .catch(() => setConnections([]))
    }
  }, [needsConnections, connections])

  useEffect(() => {
    if (needsSecrets && globalSecrets === null) {
      apiClient.get<PickableSecret[]>('/secrets/pickable')
        .then(setGlobalSecrets)
        .catch(() => setGlobalSecrets([]))
    }
  }, [needsSecrets, globalSecrets])

  const step = schema.steps[currentStep]
  const isLastStep = currentStep === schema.steps.length - 1

  const setValue = (key: string, value: string | number | boolean) => {
    setValues((prev) => ({ ...prev, [key]: value }))
  }

  const canAdvance = () => {
    if (!step) return false
    for (const field of step.fields) {
      if (field.required) {
        const val = values[field.key]
        if (val === undefined || val === '' || val === null) return false
      }
    }
    return true
  }

  const handleNext = () => {
    if (isLastStep) {
      onComplete(values)
    } else {
      setCurrentStep((s) => s + 1)
    }
  }

  const testConnection = async (fieldKey: string, connectionId: string) => {
    if (!connectionId) return
    setConnTests((prev) => ({ ...prev, [fieldKey]: { state: 'testing', message: '' } }))
    try {
      const res = await apiClient.post<{ success: boolean; message: string; response_time_ms?: number }>(
        `/admin/connections/${connectionId}/test`, {},
      )
      setConnTests((prev) => ({
        ...prev,
        [fieldKey]: {
          state: res.success ? 'ok' : 'fail',
          message: res.success
            ? `Connected${res.response_time_ms != null ? ` in ${res.response_time_ms}ms` : ''}`
            : res.message || 'Connection failed',
        },
      }))
    } catch {
      setConnTests((prev) => ({ ...prev, [fieldKey]: { state: 'fail', message: 'Test request failed' } }))
    }
  }

  const renderConnectionField = (field: WizardField) => {
    const list = (connections ?? []).filter(
      (c) => !field.dialect || c.dialect === field.dialect || c.kind === field.dialect,
    )
    const test = connTests[field.key]
    const selected = String(values[field.key] ?? '')
    return (
      <div className="flex flex-col gap-1.5">
        {connections === null ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 size={12} className="animate-spin" /> Loading connections…
          </div>
        ) : list.length === 0 ? (
          <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-300">
            No {field.dialect ? `${field.dialect} ` : ''}connections available. Ask an admin to
            create one under Admin → Connections, then come back here.
          </p>
        ) : (
          <div className="flex gap-2">
            <select
              value={selected}
              onChange={(e) => setValue(field.key, e.target.value)}
              className="flex-1 rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-1 focus:ring-primary"
            >
              <option value="">Select a connection...</option>
              {list.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}{c.dialect ? ` (${c.dialect})` : ''}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => testConnection(field.key, selected)}
              disabled={!selected || test?.state === 'testing'}
              className="flex items-center gap-1 rounded-lg border border-border px-2.5 py-2 text-xs font-medium text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
              title="Test this connection"
            >
              {test?.state === 'testing' ? <Loader2 size={12} className="animate-spin" /> : <PlugZap size={12} />}
              Test
            </button>
          </div>
        )}
        {test && test.state !== 'testing' && (
          <p className={cn('text-xs', test.state === 'ok' ? 'text-success' : 'text-destructive')}>
            {test.message}
          </p>
        )}
      </div>
    )
  }

  const renderGlobalSecretField = (field: WizardField) => (
    globalSecrets === null ? (
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 size={12} className="animate-spin" /> Loading secrets…
      </div>
    ) : globalSecrets.length === 0 ? (
      <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-300">
        No global secrets available. Ask an admin to add one under Admin → Secrets.
      </p>
    ) : (
      <select
        value={String(values[field.key] ?? '')}
        onChange={(e) => setValue(field.key, e.target.value)}
        className="rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-1 focus:ring-primary"
      >
        <option value="">Select a secret...</option>
        {globalSecrets.map((s) => (
          <option key={s.id} value={s.id}>
            {s.name}{s.is_set ? '' : ' (no value set)'}
          </option>
        ))}
      </select>
    )
  )

  return (
    <div className="flex flex-col gap-6">
      {/* Header */}
      <div>
        <h2 className="text-lg font-semibold">{schema.title}</h2>
        {schema.description && (
          <p className="mt-1 text-sm text-muted-foreground">{schema.description}</p>
        )}
      </div>

      {/* Step indicator */}
      {schema.steps.length > 1 && (
        <div className="flex items-center gap-2">
          {schema.steps.map((s, i) => (
            <div key={i} className="flex items-center gap-2">
              <div
                className={cn(
                  'flex h-7 w-7 items-center justify-center rounded-full text-xs font-medium',
                  i === currentStep
                    ? 'bg-primary text-primary-foreground'
                    : i < currentStep
                      ? 'bg-success/20 text-success'
                      : 'bg-muted text-muted-foreground'
                )}
              >
                {i < currentStep ? <Check size={12} /> : i + 1}
              </div>
              <span className={cn(
                'text-xs',
                i === currentStep ? 'font-medium text-foreground' : 'text-muted-foreground'
              )}>
                {s.title}
              </span>
              {i < schema.steps.length - 1 && (
                <div className="mx-1 h-px w-6 bg-border" />
              )}
            </div>
          ))}
        </div>
      )}

      {/* Fields */}
      <div className="flex flex-col gap-4">
        {step?.description && (
          <p className="text-sm text-muted-foreground">{step.description}</p>
        )}
        {step?.fields.map((field) => (
          <div key={field.key} className="flex flex-col gap-1.5">
            <label className="text-sm font-medium">
              {field.label}
              {field.required && <span className="ml-1 text-destructive">*</span>}
            </label>
            {field.description && (
              <p className="text-xs text-muted-foreground">{field.description}</p>
            )}

            {field.type === 'boolean' ? (
              <button
                onClick={() => setValue(field.key, !values[field.key])}
                className={cn(
                  'relative h-6 w-11 rounded-full transition-colors',
                  values[field.key] ? 'bg-primary' : 'bg-muted'
                )}
              >
                <span
                  className={cn(
                    'absolute top-0.5 block h-5 w-5 rounded-full bg-white transition-transform',
                    values[field.key] ? 'translate-x-5' : 'translate-x-0.5'
                  )}
                />
              </button>
            ) : field.type === 'connection' ? (
              renderConnectionField(field)
            ) : field.type === 'global_secret' ? (
              renderGlobalSecretField(field)
            ) : field.type === 'select' && field.options ? (
              <select
                value={String(values[field.key] ?? '')}
                onChange={(e) => setValue(field.key, e.target.value)}
                className="rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-1 focus:ring-primary"
              >
                <option value="">Select...</option>
                {field.options.map((opt) => (
                  <option key={opt} value={opt}>{opt}</option>
                ))}
              </select>
            ) : (
              <input
                type={
                  field.type === 'secret' ? 'password' :
                  field.type === 'number' ? 'number' :
                  field.type === 'url' ? 'url' :
                  'text'
                }
                value={String(values[field.key] ?? '')}
                onChange={(e) => setValue(
                  field.key,
                  field.type === 'number' ? Number(e.target.value) : e.target.value
                )}
                placeholder={field.placeholder}
                className="rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-1 focus:ring-primary"
              />
            )}
          </div>
        ))}
      </div>

      {/* Navigation */}
      <div className="flex items-center justify-between">
        <div>
          {currentStep > 0 && (
            <button
              onClick={() => setCurrentStep((s) => s - 1)}
              className="flex items-center gap-1 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:text-foreground"
            >
              <ChevronLeft size={14} />
              Back
            </button>
          )}
          {currentStep === 0 && onCancel && (
            <button
              onClick={onCancel}
              className="rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:text-foreground"
            >
              Cancel
            </button>
          )}
        </div>
        <button
          onClick={handleNext}
          disabled={!canAdvance()}
          className="flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
        >
          {isLastStep ? (
            <>
              <Check size={14} />
              Complete Setup
            </>
          ) : (
            <>
              Next
              <ChevronRight size={14} />
            </>
          )}
        </button>
      </div>
    </div>
  )
}
