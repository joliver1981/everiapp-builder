import { useState, useEffect } from 'react'
import { PageHeader } from '@/components/layout/PageHeader'
import { Pencil, Plus, Trash2, Loader2, Key } from 'lucide-react'
import { apiClient } from '@/api/client'
import type { Secret } from '@/types'
import { cn } from '@/lib/utils'

const CATEGORIES = [
  { value: 'ai_provider', label: 'AI Provider' },
  { value: 'agent_token', label: 'Agent Token (deployment)' },
  { value: 'ssh_private_key', label: 'SSH Private Key (deployment)' },
  { value: 'database', label: 'Database' },
  { value: 'smtp', label: 'SMTP / Email' },
  { value: 'integration', label: 'Integration' },
  { value: 'custom', label: 'Custom' },
]

export function AdminSecretsPage() {
  const [secrets, setSecrets] = useState<Secret[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [formData, setFormData] = useState({ name: '', category: 'custom', description: '', value: '' })
  const [isSaving, setIsSaving] = useState(false)
  const [filterCategory, setFilterCategory] = useState<string>('')

  // Edit-secret modal state — name + category are immutable (identity).
  const [editing, setEditing] = useState<Secret | null>(null)
  const [editDesc, setEditDesc] = useState('')
  const [editValue, setEditValue] = useState('')  // empty = don't change value

  const fetchSecrets = async () => {
    setIsLoading(true)
    try {
      const url = filterCategory ? `/secrets?category=${filterCategory}` : '/secrets'
      const data = await apiClient.get<Secret[]>(url)
      setSecrets(data)
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => { fetchSecrets() }, [filterCategory])

  const handleCreate = async () => {
    setIsSaving(true)
    try {
      await apiClient.post('/secrets', formData)
      setShowForm(false)
      setFormData({ name: '', category: 'custom', description: '', value: '' })
      fetchSecrets()
    } finally {
      setIsSaving(false)
    }
  }

  const openEdit = (secret: Secret) => {
    setEditing(secret)
    setEditDesc(secret.description || '')
    setEditValue('')
  }

  const closeEdit = () => {
    setEditing(null)
    setEditDesc('')
    setEditValue('')
  }

  const handleEditSubmit = async () => {
    if (!editing) return
    setIsSaving(true)
    try {
      // Only send fields the user actually changed. An empty value means
      // "don't touch the stored value"; an explicit value replaces it.
      const body: Record<string, unknown> = {}
      if (editDesc !== editing.description) body.description = editDesc
      if (editValue) body.value = editValue
      if (Object.keys(body).length === 0) {
        closeEdit()
        return
      }
      await apiClient.put(`/secrets/${editing.id}`, body)
      closeEdit()
      fetchSecrets()
    } finally {
      setIsSaving(false)
    }
  }

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this secret?')) return
    await apiClient.delete(`/secrets/${id}`)
    fetchSecrets()
  }

  return (
    <div>
      <PageHeader
        title="Secrets Management"
        description="Manage platform-wide secrets and encrypted configuration"
        actions={
          <button
            onClick={() => setShowForm(true)}
            className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          >
            <Plus size={16} />
            Add Secret
          </button>
        }
      />

      <div className="p-8">
        {/* Category filter */}
        <div className="mb-6 flex gap-2">
          <button
            onClick={() => setFilterCategory('')}
            className={cn(
              'rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              !filterCategory ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:text-foreground'
            )}
          >
            All
          </button>
          {CATEGORIES.map((cat) => (
            <button
              key={cat.value}
              onClick={() => setFilterCategory(cat.value)}
              className={cn(
                'rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
                filterCategory === cat.value ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:text-foreground'
              )}
            >
              {cat.label}
            </button>
          ))}
        </div>

        {/* Create form */}
        {showForm && (
          <div className="mb-6 rounded-xl border border-border bg-card p-6">
            <h3 className="mb-4 text-sm font-semibold">New Secret</h3>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">Name</label>
                <input
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  placeholder="e.g., openai_api_key"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">Category</label>
                <select
                  value={formData.category}
                  onChange={(e) => setFormData({ ...formData, category: e.target.value })}
                  className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                >
                  {CATEGORIES.map((cat) => (
                    <option key={cat.value} value={cat.value}>{cat.label}</option>
                  ))}
                </select>
              </div>
              <div className="col-span-2">
                <label className="mb-1 block text-xs font-medium text-muted-foreground">Description</label>
                <input
                  value={formData.description}
                  onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                  className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  placeholder="What is this secret used for?"
                />
              </div>
              <div className="col-span-2">
                <label className="mb-1 block text-xs font-medium text-muted-foreground">Value</label>
                <input
                  type="password"
                  value={formData.value}
                  onChange={(e) => setFormData({ ...formData, value: e.target.value })}
                  className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  placeholder="Secret value (will be encrypted)"
                />
              </div>
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => setShowForm(false)}
                className="rounded-lg px-4 py-2 text-sm text-muted-foreground hover:text-foreground"
              >
                Cancel
              </button>
              <button
                onClick={handleCreate}
                disabled={!formData.name || isSaving}
                className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {isSaving && <Loader2 size={14} className="animate-spin" />}
                Save Secret
              </button>
            </div>
          </div>
        )}

        {/* Secrets list */}
        {isLoading ? (
          <div className="flex justify-center py-12">
            <Loader2 size={24} className="animate-spin text-muted-foreground" />
          </div>
        ) : secrets.length === 0 ? (
          <div className="rounded-xl border border-border bg-card p-12 text-center">
            <Key size={40} className="mx-auto text-muted-foreground/30" />
            <p className="mt-4 text-muted-foreground">No secrets configured yet</p>
          </div>
        ) : (
          <div className="space-y-2">
            {secrets.map((secret) => (
              <div
                key={secret.id}
                className="flex items-center justify-between rounded-xl border border-border bg-card px-6 py-4"
              >
                <div className="flex items-center gap-4">
                  <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
                    <Key size={18} className="text-primary" />
                  </div>
                  <div>
                    <h3 className="text-sm font-medium">{secret.name}</h3>
                    <p className="text-xs text-muted-foreground">
                      {secret.description || secret.category}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-4">
                  <span className="rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                    {CATEGORIES.find((c) => c.value === secret.category)?.label || secret.category}
                  </span>
                  <span className={cn(
                    'text-xs',
                    secret.is_set ? 'text-success' : 'text-warning'
                  )}>
                    {secret.is_set ? 'Set' : 'Not set'}
                  </span>
                  <button
                    onClick={() => openEdit(secret)}
                    className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                    title="Edit secret"
                  >
                    <Pencil size={14} />
                  </button>
                  <button
                    onClick={() => handleDelete(secret.id)}
                    className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
                    title="Delete secret"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Edit secret modal */}
        {editing && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
            <div className="w-full max-w-md rounded-2xl border border-border bg-card p-6">
              <h2 className="text-lg font-semibold">Edit Secret</h2>
              <p className="mt-1 text-xs text-muted-foreground">
                Name &amp; category are immutable. Leave the value field empty to keep the existing value.
              </p>
              <div className="mt-4 space-y-3">
                <div>
                  <label className="mb-1 block text-xs font-medium text-muted-foreground">Name</label>
                  <input
                    value={editing.name}
                    readOnly
                    className="w-full rounded-lg border border-input bg-muted px-3 py-2 text-sm text-muted-foreground"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-muted-foreground">Description</label>
                  <input
                    value={editDesc}
                    onChange={(e) => setEditDesc(e.target.value)}
                    className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-muted-foreground">
                    New value <span className="text-muted-foreground/70">(leave empty to keep current)</span>
                  </label>
                  <input
                    type="password"
                    value={editValue}
                    onChange={(e) => setEditValue(e.target.value)}
                    placeholder={editing.is_set ? '••••••••' : 'No value set'}
                    className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  />
                </div>
              </div>
              <div className="mt-4 flex justify-end gap-2">
                <button
                  onClick={closeEdit}
                  className="rounded-lg px-4 py-2 text-sm text-muted-foreground hover:text-foreground"
                >
                  Cancel
                </button>
                <button
                  onClick={handleEditSubmit}
                  disabled={isSaving}
                  className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                >
                  {isSaving && <Loader2 size={14} className="animate-spin" />}
                  Save changes
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
