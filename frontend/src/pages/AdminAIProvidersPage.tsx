import { useState, useEffect, useRef } from 'react'
import { PageHeader } from '@/components/layout/PageHeader'
import { Plus, Pencil, Trash2, Loader2, Bot, CheckCircle, XCircle, Zap, Play, SlidersHorizontal } from 'lucide-react'
import { apiClient } from '@/api/client'
import type { AIProvider, AIPurposeDefault } from '@/types'
import { cn } from '@/lib/utils'

// Suggested CURRENT models (refreshed mid-2026). The model field is a free-type
// combobox, so an operator can enter any id their key supports — these are just
// recommended starting points, listed most-capable first (the first one becomes
// the default when its provider is selected).
const PROVIDER_TYPES = [
  { value: 'openai', label: 'OpenAI', models: ['gpt-5.5', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5.4-nano'] },
  { value: 'anthropic', label: 'Anthropic', models: ['claude-opus-4-8', 'claude-sonnet-4-6', 'claude-haiku-4-5-20251001', 'claude-fable-5'] },
  { value: 'azure', label: 'Azure OpenAI', models: ['gpt-5.5', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5.4-nano'] },
  { value: 'google', label: 'Google AI', models: ['gemini-3.1-pro-preview', 'gemini-3.5-flash', 'gemini-3.1-flash-lite', 'gemini-2.5-pro', 'gemini-2.5-flash'] },
  { value: 'ollama', label: 'Ollama (Local)', models: ['qwen3-coder:30b', 'qwen2.5-coder:32b', 'deepseek-r1:32b', 'gpt-oss:20b', 'llama3.3:70b'] },
]

/** Combobox: pick from presets or type a custom model name. */
function ModelCombobox({
  value,
  onChange,
  suggestions,
}: {
  value: string
  onChange: (v: string) => void
  suggestions: string[]
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // Close dropdown when clicking outside
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const filtered = suggestions.filter((m) =>
    m.toLowerCase().includes(value.toLowerCase()),
  )

  return (
    <div ref={ref} className="relative">
      <input
        value={value}
        onChange={(e) => { onChange(e.target.value); setOpen(true) }}
        onFocus={() => setOpen(true)}
        placeholder="Type or pick a model..."
        className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
      />
      {open && filtered.length > 0 && (
        <ul className="absolute z-50 mt-1 max-h-48 w-full overflow-auto rounded-lg border border-border bg-popover py-1 shadow-lg">
          {filtered.map((m) => (
            <li key={m}>
              <button
                type="button"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => { onChange(m); setOpen(false) }}
                className={cn(
                  'w-full px-3 py-1.5 text-left text-sm hover:bg-accent',
                  m === value && 'bg-accent/50 font-medium',
                )}
              >
                {m}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// Human wording for where a purpose's effective provider came from.
const PURPOSE_SOURCE_LABELS: Record<string, string> = {
  pinned: 'pinned',
  legacy_default: 'provider default flag',
  inherited_generation: 'inherited from App generation',
  first_active: 'first active provider',
}

export function AdminAIProvidersPage() {
  const [providers, setProviders] = useState<AIProvider[]>([])
  const [purposes, setPurposes] = useState<AIPurposeDefault[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)  // null = create mode
  const [testingId, setTestingId] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null)
  const [isSaving, setIsSaving] = useState(false)
  const [keySource, setKeySource] = useState<string | null>(null)
  const [formData, setFormData] = useState({
    name: '',
    provider_type: 'openai',
    api_key: '',
    base_url: '',
    default_model: 'gpt-5.5',
    is_default_generation: false,
    is_default_toggle: false,
  })

  // Local drafts for the per-purpose model override inputs (committed on blur).
  const [modelDrafts, setModelDrafts] = useState<Record<string, string>>({})
  // One purpose PUT in flight at a time: all purpose controls disable while
  // saving, so out-of-order responses can't clobber state or drafts.
  const [pinSaving, setPinSaving] = useState(false)

  const fetchProviders = async () => {
    setIsLoading(true)
    try {
      const data = await apiClient.get<AIProvider[]>('/admin/ai-providers')
      setProviders(data)
      // Separate try: a purposes failure (e.g. transient 500) must not blank
      // the provider list — that's the page the admin recovers with.
      try {
        applyPurposes(await apiClient.get<AIPurposeDefault[]>('/admin/ai-providers/purposes'))
      } catch {
        setPurposes([])
      }
    } finally {
      setIsLoading(false)
    }
  }

  const applyPurposes = (rows: AIPurposeDefault[]) => {
    setPurposes(rows)
    setModelDrafts(Object.fromEntries(rows.map((row) => [row.purpose, row.model || ''])))
  }

  const savePin = async (purpose: string, providerId: string | null, model: string | null) => {
    setPinSaving(true)
    try {
      applyPurposes(await apiClient.put<AIPurposeDefault[]>(
        `/admin/ai-providers/purposes/${purpose}`,
        { provider_id: providerId, model },
      ))
    } catch (e: any) {
      setTestResult({ success: false, message: `Saving purpose default failed: ${e.message}` })
      // Resync so the controls reflect what's actually stored.
      try {
        applyPurposes(await apiClient.get<AIPurposeDefault[]>('/admin/ai-providers/purposes'))
      } catch { /* keep whatever we had */ }
    } finally {
      setPinSaving(false)
    }
  }

  const handlePinProvider = (purpose: string, providerId: string) =>
    // Switching providers drops any model override — it was provider-specific.
    savePin(purpose, providerId || null, null)

  const handleModelOverrideBlur = (row: AIPurposeDefault) => {
    if (!row.provider_id) return
    const draft = (modelDrafts[row.purpose] || '').trim()
    if (draft === (row.model || '')) return
    return savePin(row.purpose, row.provider_id, draft || null)
  }

  useEffect(() => {
    fetchProviders()
    apiClient.get<{ key_source: string }>('/admin/system/encryption')
      .then((data) => setKeySource(data.key_source))
      .catch(() => {})
  }, [])

  const EMPTY_FORM = {
    name: '', provider_type: 'openai', api_key: '', base_url: '',
    default_model: 'gpt-5.5', is_default_generation: false, is_default_toggle: false,
  }

  const openCreate = () => {
    setEditingId(null)
    setFormData(EMPTY_FORM)
    setTestResult(null)
    setShowForm(true)
  }

  const openEdit = (provider: AIProvider) => {
    setEditingId(provider.id)
    setFormData({
      name: provider.name,
      provider_type: provider.provider_type,
      api_key: '',  // never round-trip the encrypted key; empty = "don't change"
      base_url: provider.base_url || '',
      default_model: provider.default_model || '',
      is_default_generation: provider.is_default_generation,
      is_default_toggle: provider.is_default_toggle,
    })
    setTestResult(null)
    setShowForm(true)
  }

  const closeForm = () => {
    setShowForm(false)
    setEditingId(null)
    setFormData(EMPTY_FORM)
  }

  const handleSubmit = async (andTest = false) => {
    setIsSaving(true)
    setTestResult(null)
    try {
      let providerId: string
      if (editingId) {
        // PUT only the fields the user changed; in particular skip api_key
        // unless they actually typed a new one.
        const body: Record<string, unknown> = {
          name: formData.name,
          base_url: formData.base_url,
          default_model: formData.default_model,
          is_default_generation: formData.is_default_generation,
          is_default_toggle: formData.is_default_toggle,
        }
        if (formData.api_key) body.api_key = formData.api_key
        const updated = await apiClient.put<AIProvider>(`/admin/ai-providers/${editingId}`, body)
        providerId = updated.id
      } else {
        const created = await apiClient.post<AIProvider>('/admin/ai-providers', formData)
        providerId = created.id
      }
      closeForm()
      await fetchProviders()
      if (andTest) handleTest(providerId)
    } catch (e: any) {
      setTestResult({ success: false, message: `Save failed: ${e.message}` })
    } finally {
      setIsSaving(false)
    }
  }

  const handleTest = async (id: string) => {
    setTestingId(id)
    setTestResult(null)
    try {
      const result = await apiClient.post<{ success: boolean; message: string; response_time_ms?: number }>(
        `/admin/ai-providers/${id}/test`
      )
      setTestResult(result)
      fetchProviders()
    } catch (e: any) {
      setTestResult({ success: false, message: e.message })
    } finally {
      setTestingId(null)
    }
  }

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this AI provider?')) return
    await apiClient.delete(`/admin/ai-providers/${id}`)
    fetchProviders()
  }

  const handleSetDefault = async (id: string, field: 'is_default_generation' | 'is_default_toggle') => {
    await apiClient.put(`/admin/ai-providers/${id}`, { [field]: true })
    fetchProviders()
  }

  const selectedProviderModels = PROVIDER_TYPES.find((p) => p.value === formData.provider_type)?.models || []

  return (
    <div>
      <PageHeader
        title="AI Providers"
        description="Configure LLM providers and API keys for app generation and AI Toggle"
        actions={
          <button
            onClick={openCreate}
            className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          >
            <Plus size={16} />
            Add Provider
          </button>
        }
      />

      <div className="p-8">
        {/* Encryption key warning */}
        {keySource === 'random' && (
          <div className="mb-6 flex items-start gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4">
            <XCircle size={18} className="mt-0.5 shrink-0 text-destructive" />
            <div>
              <p className="text-sm font-medium text-destructive">Encryption key is temporary</p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                API keys are encrypted with a random key that will be lost on server restart.
                Set <code className="rounded bg-muted px-1">MASTER_ENCRYPTION_KEY</code> in your .env file for persistence.
              </p>
            </div>
          </div>
        )}
        {keySource === 'machine' && (
          <div className="mb-6 flex items-start gap-3 rounded-xl border border-border bg-muted/30 p-4">
            <CheckCircle size={18} className="mt-0.5 shrink-0 text-success" />
            <div>
              <p className="text-sm font-medium">Encryption key derived from machine ID</p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                API keys are encrypted with a stable key tied to this machine. If you move to a different server,
                set <code className="rounded bg-muted px-1">MASTER_ENCRYPTION_KEY</code> in .env or re-enter all API keys.
              </p>
            </div>
          </div>
        )}

        {/* Create / edit form */}
        {showForm && (
          <div className="mb-6 rounded-xl border border-border bg-card p-6">
            <h3 className="mb-4 text-sm font-semibold">{editingId ? 'Edit AI Provider' : 'New AI Provider'}</h3>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">Name</label>
                <input
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  placeholder="e.g., Production OpenAI"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">Provider</label>
                <select
                  value={formData.provider_type}
                  onChange={(e) => {
                    const type = e.target.value
                    const models = PROVIDER_TYPES.find((p) => p.value === type)?.models || []
                    setFormData({ ...formData, provider_type: type, default_model: models[0] || '' })
                  }}
                  disabled={!!editingId}
                  className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-60"
                  title={editingId ? 'Provider type is immutable — delete and recreate to change it.' : undefined}
                >
                  {PROVIDER_TYPES.map((p) => (
                    <option key={p.value} value={p.value}>{p.label}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">
                  API Key {editingId && <span className="text-muted-foreground/70">(leave empty to keep current)</span>}
                </label>
                <input
                  type="password"
                  value={formData.api_key}
                  onChange={(e) => setFormData({ ...formData, api_key: e.target.value })}
                  className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  placeholder={editingId ? '••••••••' : 'sk-...'}
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">Default Model</label>
                <ModelCombobox
                  value={formData.default_model}
                  onChange={(v) => setFormData({ ...formData, default_model: v })}
                  suggestions={selectedProviderModels}
                />
              </div>
              {formData.provider_type === 'azure' && (
                <div className="col-span-2">
                  <label className="mb-1 block text-xs font-medium text-muted-foreground">Base URL</label>
                  <input
                    value={formData.base_url}
                    onChange={(e) => setFormData({ ...formData, base_url: e.target.value })}
                    className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                    placeholder="https://your-resource.openai.azure.com/"
                  />
                </div>
              )}
              <div className="col-span-2 flex gap-6">
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={formData.is_default_generation}
                    onChange={(e) => setFormData({ ...formData, is_default_generation: e.target.checked })}
                    className="rounded border-input"
                  />
                  Default for app generation
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={formData.is_default_toggle}
                    onChange={(e) => setFormData({ ...formData, is_default_toggle: e.target.checked })}
                    className="rounded border-input"
                  />
                  Default for AI Toggle
                </label>
              </div>
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={closeForm}
                className="rounded-lg px-4 py-2 text-sm text-muted-foreground hover:text-foreground"
              >
                Cancel
              </button>
              <button
                onClick={() => handleSubmit(false)}
                disabled={!formData.name || (!editingId && !formData.api_key) || isSaving}
                className="flex items-center gap-2 rounded-lg border border-border bg-secondary px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
              >
                {isSaving && <Loader2 size={14} className="animate-spin" />}
                {editingId ? 'Save changes' : 'Save'}
              </button>
              <button
                onClick={() => handleSubmit(true)}
                disabled={!formData.name || (!editingId && !formData.api_key) || !formData.default_model || isSaving}
                className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {isSaving && <Loader2 size={14} className="animate-spin" />}
                <Play size={14} />
                {editingId ? 'Save & Test' : 'Save & Test'}
              </button>
            </div>
          </div>
        )}

        {/* Test result toast */}
        {testResult && (
          <div className={cn(
            'mb-4 flex items-center gap-3 rounded-xl border px-4 py-3 text-sm',
            testResult.success
              ? 'border-success/20 bg-success/5 text-success'
              : 'border-destructive/20 bg-destructive/5 text-destructive'
          )}>
            {testResult.success ? <CheckCircle size={16} /> : <XCircle size={16} />}
            {testResult.message}
            <button onClick={() => setTestResult(null)} className="ml-auto text-xs opacity-50 hover:opacity-100">
              Dismiss
            </button>
          </div>
        )}

        {/* Providers list */}
        {isLoading ? (
          <div className="flex justify-center py-12">
            <Loader2 size={24} className="animate-spin text-muted-foreground" />
          </div>
        ) : providers.length === 0 ? (
          <div className="rounded-xl border border-border bg-card p-12 text-center">
            <Bot size={40} className="mx-auto text-muted-foreground/30" />
            <p className="mt-4 text-muted-foreground">No AI providers configured</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Add a provider to start building apps with AI
            </p>
          </div>
        ) : (
          <>
          <div className="space-y-3">
            {providers.map((provider) => (
              <div
                key={provider.id}
                className="rounded-xl border border-border bg-card px-6 py-4"
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-4">
                    <div className={cn(
                      'flex h-10 w-10 items-center justify-center rounded-lg',
                      provider.is_active ? 'bg-primary/10' : 'bg-muted'
                    )}>
                      <Bot size={18} className={provider.is_active ? 'text-primary' : 'text-muted-foreground'} />
                    </div>
                    <div>
                      <div className="flex items-center gap-2">
                        <h3 className="text-sm font-medium">{provider.name}</h3>
                        {provider.is_default_generation && (
                          <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                            Generation
                          </span>
                        )}
                        {provider.is_default_toggle && (
                          <span className="rounded bg-success/10 px-1.5 py-0.5 text-[10px] font-medium text-success">
                            AI Toggle
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground">
                        {PROVIDER_TYPES.find((p) => p.value === provider.provider_type)?.label || provider.provider_type}
                        {' '} / {provider.default_model}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {provider.last_verified && (
                      <span className="text-xs text-success">Verified</span>
                    )}
                    <button
                      onClick={() => handleTest(provider.id)}
                      disabled={testingId === provider.id}
                      className="flex items-center gap-1 rounded-lg px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                    >
                      {testingId === provider.id ? (
                        <Loader2 size={12} className="animate-spin" />
                      ) : (
                        <Play size={12} />
                      )}
                      Test
                    </button>
                    <button
                      onClick={() => handleSetDefault(provider.id, 'is_default_generation')}
                      className={cn(
                        'rounded-lg px-3 py-1.5 text-xs transition-colors',
                        provider.is_default_generation
                          ? 'bg-primary/10 text-primary'
                          : 'text-muted-foreground hover:bg-accent hover:text-foreground'
                      )}
                      title={provider.is_default_generation
                        ? 'Default for app generation (a Purpose defaults pin below overrides this)'
                        : 'Make default for app generation (a Purpose defaults pin below overrides this)'}
                    >
                      <Zap size={12} />
                    </button>
                    <button
                      onClick={() => openEdit(provider)}
                      className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                      title="Edit provider"
                    >
                      <Pencil size={14} />
                    </button>
                    <button
                      onClick={() => handleDelete(provider.id)}
                      className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
                      title="Delete provider"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* Purpose defaults: which provider answers each kind of platform LLM call.
              Unpinned purposes inherit the App generation default, so this section
              is optional — it exists for "cheap model for X, big model for Y" setups. */}
          <div className="mt-8">
            <div className="mb-3 flex items-center gap-2">
              <SlidersHorizontal size={16} className="text-muted-foreground" />
              <h3 className="text-sm font-semibold">Purpose defaults</h3>
            </div>
            <p className="mb-4 text-xs text-muted-foreground">
              Pin a provider (and optionally a model) per kind of AI call. Anything left
              on Automatic inherits the app-generation default.
            </p>
            <div className="space-y-3">
              {purposes.map((row) => (
                <div key={row.purpose} className="rounded-xl border border-border bg-card px-6 py-4">
                  <div className="flex flex-wrap items-center gap-4">
                    <div className="min-w-64 flex-1">
                      <h4 className="text-sm font-medium">{row.label}</h4>
                      <p className="mt-0.5 text-xs text-muted-foreground">{row.description}</p>
                    </div>
                    <div className="flex items-center gap-2">
                      <select
                        value={row.provider_id || ''}
                        onChange={(e) => handlePinProvider(row.purpose, e.target.value)}
                        disabled={pinSaving}
                        className="rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
                      >
                        <option value="">Automatic</option>
                        {providers.map((p) => (
                          <option key={p.id} value={p.id}>{p.name}</option>
                        ))}
                        {/* Pins are cleared when a provider is deleted, but rows
                            written before that existed can still dangle — keep
                            the state legible instead of rendering blank. */}
                        {row.provider_id && !providers.some((p) => p.id === row.provider_id) && (
                          <option value={row.provider_id}>(deleted provider)</option>
                        )}
                      </select>
                      <input
                        value={modelDrafts[row.purpose] || ''}
                        onChange={(e) => setModelDrafts({ ...modelDrafts, [row.purpose]: e.target.value })}
                        onBlur={() => handleModelOverrideBlur(row)}
                        disabled={pinSaving || !row.provider_id || !providers.some((p) => p.id === row.provider_id)}
                        placeholder="provider's default model"
                        title={row.provider_id ? 'Optional model override for this purpose' : 'Pin a provider to override its model'}
                        className="w-52 rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
                      />
                    </div>
                  </div>
                  {row.effective ? (
                    <p className="mt-2 text-xs text-muted-foreground">
                      Using <span className="font-medium text-foreground">{row.effective.provider_name}</span>
                      {' / '}{row.effective.model}
                      <span className="ml-1.5 rounded bg-muted px-1.5 py-0.5 text-[10px]">
                        {PURPOSE_SOURCE_LABELS[row.effective.source] || row.effective.source}
                      </span>
                    </p>
                  ) : (
                    <p className="mt-2 text-xs text-destructive">
                      No active provider resolves for this purpose.
                    </p>
                  )}
                </div>
              ))}
            </div>
          </div>
          </>
        )}
      </div>
    </div>
  )
}
