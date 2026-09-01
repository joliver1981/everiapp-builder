import { useState, useRef, useEffect, useCallback, type FormEvent, type DragEvent, type ClipboardEvent } from 'react'
import { Send, Loader2, Sparkles, Wifi, WifiOff, ChevronDown, Paperclip, ImagePlus, AlertTriangle } from 'lucide-react'
import { useChatStore } from '@/stores/chatStore'
import { apiClient } from '@/api/client'
import type { ChatAttachment } from '@/types'
import { MessageBubble } from './MessageBubble'
import { VerifyStatusBar } from './VerifyStatusBar'
import { AttachmentThumb, type AttachmentStatus } from './AttachmentChips'
import { cn } from '@/lib/utils'

interface AIProviderOption {
  id: string
  name: string
  provider_type: string
  default_model: string
  is_default_generation: boolean
  // Input-modality hints from the backend: true/false when the model is known
  // to litellm, null for brand-new ids (treated as "try it").
  supports_vision?: boolean | null
  supports_pdf?: boolean | null
}

interface ChatPanelProps {
  appId: string
}

interface PromptTemplate {
  id: string
  title: string
  description: string
  category: string
  body: string
}

// --- Attachments (screenshots, images, PDFs, text files) ---------------------
// Mirrors backend/src/ai/attachments.py classify_upload(): the backend is the
// authority (it refuses with 415), this just gives instant feedback + the
// file-picker accept list.
type AttachmentKind = ChatAttachment['kind']

const IMAGE_MIMES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])
const IMAGE_EXT: Record<string, string> = {
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp',
}
const TEXT_EXTS = [
  'txt', 'md', 'markdown', 'csv', 'tsv', 'json', 'jsonl', 'yaml', 'yml', 'xml', 'html', 'htm', 'css',
  'scss', 'js', 'jsx', 'ts', 'tsx', 'mjs', 'cjs', 'py', 'sql', 'sh', 'ps1', 'bat', 'ini', 'toml', 'cfg',
  'conf', 'env', 'log', 'rtf', 'svg', 'graphql', 'proto', 'java', 'cs', 'go', 'rs', 'rb', 'php', 'c',
  'h', 'cpp', 'hpp', 'kt', 'swift', 'r',
]
const TEXT_EXT_SET = new Set(TEXT_EXTS)
const ACCEPT_ATTR = [
  ...IMAGE_MIMES, 'application/pdf', 'text/*', ...TEXT_EXTS.map((e) => `.${e}`),
].join(',')
const ACCEPTED_DESCRIPTION = 'images (PNG, JPEG, GIF, WebP), PDFs, and text/code files'

function classifyFile(file: File): { kind: AttachmentKind; mime: string } | null {
  const ext = (file.name.split('.').pop() || '').toLowerCase()
  const ct = (file.type || '').split(';')[0].trim().toLowerCase()
  if (IMAGE_MIMES.has(ct)) return { kind: 'image', mime: ct }
  if (IMAGE_EXT[ext]) return { kind: 'image', mime: IMAGE_EXT[ext] }
  if (ct === 'application/pdf' || ext === 'pdf') return { kind: 'pdf', mime: 'application/pdf' }
  if (TEXT_EXT_SET.has(ext) || ct.startsWith('text/') || ct === 'application/json' || ct === 'application/xml') {
    return { kind: 'text', mime: ct && ct !== 'application/octet-stream' ? ct : 'text/plain' }
  }
  return null
}

// Providers downscale anything past ~2048px on their side and reject images
// over ~5 MB outright, so an oversized screenshot is shrunk in the browser to
// what the model would see anyway — and the chip says so. Smaller images are
// sent untouched.
const MAX_IMAGE_EDGE = 2048
const MAX_IMAGE_BYTES = 4.5 * 1024 * 1024

async function normalizeImage(file: File): Promise<{ file: File; note?: string }> {
  if (!file.type.startsWith('image/') || file.type === 'image/gif') return { file }
  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file)
  } catch {
    return { file }
  }
  const longest = Math.max(bitmap.width, bitmap.height)
  if (file.size <= MAX_IMAGE_BYTES && longest <= MAX_IMAGE_EDGE) {
    bitmap.close()
    return { file }
  }
  const keepAlpha = file.type === 'image/png' || file.type === 'image/webp'
  let scale = Math.min(1, MAX_IMAGE_EDGE / longest)
  for (let attempt = 0; attempt < 4; attempt++) {
    const canvas = document.createElement('canvas')
    canvas.width = Math.max(1, Math.round(bitmap.width * scale))
    canvas.height = Math.max(1, Math.round(bitmap.height * scale))
    const ctx = canvas.getContext('2d')
    if (!ctx) break
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
    const type = keepAlpha && attempt === 0 ? 'image/png' : 'image/jpeg'
    const blob = await new Promise<Blob | null>((res) => canvas.toBlob(res, type, 0.9))
    if (blob && blob.size <= MAX_IMAGE_BYTES) {
      const base = file.name.replace(/\.[^.]+$/, '') || 'image'
      const out = new File([blob], `${base}.${type === 'image/png' ? 'png' : 'jpg'}`, { type })
      const note = `resized from ${bitmap.width}×${bitmap.height} to ${canvas.width}×${canvas.height}`
      bitmap.close()
      return { file: out, note }
    }
    scale *= 0.7
  }
  bitmap.close()
  return { file } // could not shrink it — send as-is and let the provider answer
}

function errorText(e: unknown): string {
  const raw = (e as { message?: string })?.message ?? String(e)
  try {
    const j = JSON.parse(raw)
    if (j?.detail) return typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
  } catch {
    /* not JSON */
  }
  return raw
}

interface PendingAttachment extends ChatAttachment {
  key: string
  file: File
  status: AttachmentStatus
  error?: string
  note?: string
}

let pendingKey = 0

export function ChatPanel({ appId }: ChatPanelProps) {
  const [input, setInput] = useState('')
  const [providers, setProviders] = useState<AIProviderOption[]>([])
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(null)
  const [showProviderMenu, setShowProviderMenu] = useState(false)
  const [templates, setTemplates] = useState<PromptTemplate[]>([])
  const [pending, setPending] = useState<PendingAttachment[]>([])
  const [attachError, setAttachError] = useState<string | null>(null)
  const [dragActive, setDragActive] = useState(false)
  const dragDepth = useRef(0)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const providerRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const {
    messages, isStreaming, isConnected, isConnecting, connectionError, sendMessage,
    verifyProgress, verifyResult, rollbackAvailable, rollbackDraft, dismissVerifyResult,
  } = useChatStore()

  // Auto-scroll, at most once per frame. Streamed chunks arrive faster than the
  // display refreshes; calling scrollIntoView({smooth}) per chunk restarts the
  // scroll animation and forces layout each time. While streaming, jump ('auto')
  // instead of gliding — a smooth scroll never finishes between chunks anyway.
  const scrollPending = useRef(false)
  useEffect(() => {
    if (scrollPending.current) return
    scrollPending.current = true
    requestAnimationFrame(() => {
      scrollPending.current = false
      messagesEndRef.current?.scrollIntoView({
        behavior: useChatStore.getState().isStreaming ? 'auto' : 'smooth',
      })
    })
  }, [messages])

  // Load the prompt library for the empty-state starters (best-effort).
  useEffect(() => {
    apiClient
      .get<PromptTemplate[]>('/prompt-templates')
      .then(setTemplates)
      .catch(() => setTemplates([]))
  }, [])

  // Load available providers. Prefer the provider this app was last built
  // with (persisted server-side when a message uses it) so reopening the
  // builder continues with the same model; fall back to the platform default
  // when the app never chose one or its provider was since deleted.
  useEffect(() => {
    Promise.all([
      apiClient.get<AIProviderOption[]>('/ai/providers'),
      apiClient.get<{ builder_provider_id?: string | null }>(`/apps/${appId}`).catch(() => null),
    ]).then(([data, app]) => {
      setProviders(data)
      const remembered = app?.builder_provider_id
        ? data.find((p) => p.id === app.builder_provider_id)
        : undefined
      const defaultProvider = data.find((p) => p.is_default_generation)
      if (remembered) {
        setSelectedProviderId(remembered.id)
      } else if (defaultProvider) {
        setSelectedProviderId(defaultProvider.id)
      } else if (data.length > 0) {
        setSelectedProviderId(data[0].id)
      }
    }).catch(() => {
      // No providers configured
    })
  }, [appId])

  // Close provider menu when clicking outside
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (providerRef.current && !providerRef.current.contains(e.target as Node)) {
        setShowProviderMenu(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const selectedProvider = providers.find((p) => p.id === selectedProviderId)

  // --- attachment intake: file picker, drag/drop, clipboard paste ------------
  const uploadOne = useCallback(async (p: PendingAttachment) => {
    const form = new FormData()
    form.append('app_id', appId)
    form.append('files', p.file, p.name)
    try {
      const res = await apiClient.postForm<{ attachments: ChatAttachment[] }>('/ai/attachments', form)
      const a = res.attachments[0]
      setPending((prev) => prev.map((x) => (
        x.key === p.key ? { ...x, id: a.id, kind: a.kind, mime: a.mime, size: a.size, status: 'ready' } : x
      )))
    } catch (e) {
      setPending((prev) => prev.map((x) => (
        x.key === p.key ? { ...x, status: 'error', error: errorText(e) } : x
      )))
    }
  }, [appId])

  const addFiles = useCallback(async (files: File[]) => {
    if (!files.length) return
    setAttachError(null)
    const rejected: string[] = []
    for (const raw of files) {
      const cls = classifyFile(raw)
      if (!cls) {
        rejected.push(raw.name || 'file')
        continue
      }
      let file = raw
      let note: string | undefined
      if (cls.kind === 'image') {
        const norm = await normalizeImage(raw)
        file = norm.file
        note = norm.note
      }
      // Clipboard screenshots arrive as a generic "image.png" — give them a name.
      const name = /^image\.(png|jpe?g|webp|gif)$/i.test(file.name)
        ? `screenshot-${new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)}.${file.name.split('.').pop()}`
        : file.name
      const p: PendingAttachment = {
        key: `att-${++pendingKey}`,
        id: '',
        name,
        mime: cls.mime,
        kind: cls.kind,
        size: file.size,
        localUrl: cls.kind === 'image' ? URL.createObjectURL(file) : undefined,
        file,
        status: 'uploading',
        note,
      }
      setPending((prev) => [...prev, p])
      void uploadOne(p)
    }
    if (rejected.length) {
      setAttachError(`Not attached: ${rejected.join(', ')}. Supported: ${ACCEPTED_DESCRIPTION}.`)
    }
  }, [uploadOne])

  const removePending = (key: string) => {
    setPending((prev) => {
      const gone = prev.find((p) => p.key === key)
      if (gone?.localUrl) URL.revokeObjectURL(gone.localUrl)
      return prev.filter((p) => p.key !== key)
    })
  }

  const onDragEnter = (e: DragEvent) => {
    if (!Array.from(e.dataTransfer?.types ?? []).includes('Files')) return
    e.preventDefault()
    dragDepth.current += 1
    setDragActive(true)
  }
  const onDragOver = (e: DragEvent) => {
    if (!Array.from(e.dataTransfer?.types ?? []).includes('Files')) return
    e.preventDefault()
    e.dataTransfer.dropEffect = 'copy'
  }
  const onDragLeave = (e: DragEvent) => {
    if (!Array.from(e.dataTransfer?.types ?? []).includes('Files')) return
    e.preventDefault()
    dragDepth.current = Math.max(0, dragDepth.current - 1)
    if (dragDepth.current === 0) setDragActive(false)
  }
  const onDrop = (e: DragEvent) => {
    if (!Array.from(e.dataTransfer?.types ?? []).includes('Files')) return
    e.preventDefault()
    dragDepth.current = 0
    setDragActive(false)
    if (isStreaming) return
    void addFiles(Array.from(e.dataTransfer.files))
  }
  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(e.clipboardData?.files ?? [])
    if (!files.length) return
    e.preventDefault()
    void addFiles(files)
  }

  const readyAttachments = pending.filter((p) => p.status === 'ready' && p.id)
  const uploading = pending.some((p) => p.status === 'uploading')
  const hasFailed = pending.some((p) => p.status === 'error')
  const hasBinary = pending.some((p) => p.kind !== 'text')
  const modelRejectsImages = hasBinary && selectedProvider?.supports_vision === false
  const canSend = (input.trim().length > 0 || readyAttachments.length > 0)
    && !isStreaming && !uploading && !hasFailed && !modelRejectsImages

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (!canSend) return
    const attachments: ChatAttachment[] = readyAttachments.map((p) => ({
      id: p.id, name: p.name, mime: p.mime, kind: p.kind, size: p.size, localUrl: p.localUrl,
    }))
    sendMessage(appId, input.trim(), selectedProviderId, undefined, attachments)
    setInput('')
    setPending([])  // object URLs stay alive for the bubble thumbnails
    setAttachError(null)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit(e)
    }
  }

  return (
    <div
      className="relative flex h-full flex-col"
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      {/* Drop target overlay */}
      {dragActive && (
        <div className="pointer-events-none absolute inset-0 z-40 flex items-center justify-center rounded-lg border-2 border-dashed border-primary bg-primary/10">
          <div className="flex items-center gap-2 rounded-lg bg-background px-4 py-2 text-sm font-medium shadow">
            <ImagePlus size={18} className="text-primary" />
            Drop screenshots, images, PDFs or text files to attach
          </div>
        </div>
      )}

      {/* Connection status bar */}
      {(isConnecting || connectionError || !isConnected) && (
        <div className={cn(
          'flex items-center gap-2 px-4 py-2 text-xs',
          connectionError
            ? 'bg-destructive/10 text-destructive'
            : isConnecting
            ? 'bg-warning/10 text-warning'
            : 'bg-muted text-muted-foreground'
        )}>
          {isConnecting ? (
            <>
              <Loader2 size={12} className="animate-spin" />
              Connecting to AI service...
            </>
          ) : connectionError ? (
            <>
              <WifiOff size={12} />
              {connectionError}. Try refreshing the page.
            </>
          ) : (
            <>
              <WifiOff size={12} />
              Not connected to AI service
            </>
          )}
        </div>
      )}

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4">
        {messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center text-center">
            <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/10">
              <Sparkles size={32} className="text-primary" />
            </div>
            <h3 className="mt-4 text-lg font-medium">Start Building</h3>
            <p className="mt-2 max-w-sm text-sm text-muted-foreground">
              Describe the app you want to build and AI will generate it for you.
              You can iterate by chatting to refine the app — drop in screenshots,
              mockups or PDFs to show it what you mean.
            </p>
            <div className="mt-6 w-full max-w-md space-y-2">
              {templates.length > 0 && (
                <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground/70">
                  Start from a template
                </p>
              )}
              {(templates.length > 0
                ? templates.map((t) => ({ key: t.id, title: t.title, sub: t.description, body: t.body }))
                : [
                    { key: 'a', title: 'Build a sales dashboard with charts and filters', sub: '', body: 'Build a sales dashboard with charts and filters' },
                    { key: 'b', title: 'Create a task management app with kanban board', sub: '', body: 'Create a task management app with kanban board' },
                    { key: 'c', title: 'Make a data entry form with validation', sub: '', body: 'Make a data entry form with validation' },
                  ]
              ).map((s) => (
                <button
                  key={s.key}
                  onClick={() => {
                    setInput(s.body)
                    inputRef.current?.focus()
                  }}
                  className="block w-full rounded-lg border border-border px-4 py-2 text-left transition-colors hover:bg-accent hover:text-foreground"
                >
                  <span className="block text-sm text-foreground">{s.title}</span>
                  {s.sub && (
                    <span className="mt-0.5 block text-xs text-muted-foreground">{s.sub}</span>
                  )}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            {messages.map((msg) => (
              <MessageBubble key={msg.id} message={msg} />
            ))}
            {isStreaming && (
              <div className="flex items-center gap-2 px-1 text-xs text-muted-foreground">
                <span className="flex gap-1">
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-primary" style={{ animationDelay: '-0.3s' }} />
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-primary" style={{ animationDelay: '-0.15s' }} />
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-primary" />
                </span>
                AI is working… a full app can take a minute or two (writing files, then verifying).
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>
        )}
      </div>

      {/* AI self-heal status — live progress + post-turn outcome */}
      <VerifyStatusBar
        progress={verifyProgress}
        result={verifyResult}
        rollbackAvailable={rollbackAvailable}
        onRollback={rollbackDraft}
        onDismiss={dismissVerifyResult}
      />

      {/* Input area */}
      <div className="border-t border-border p-4">
        {/* Provider selector row */}
        {providers.length > 0 && (
          <div className="mb-2 flex items-center gap-2" ref={providerRef}>
            <span className="text-xs text-muted-foreground">Provider:</span>
            <div className="relative">
              <button
                type="button"
                onClick={() => setShowProviderMenu(!showProviderMenu)}
                className="flex items-center gap-1.5 rounded-md border border-border bg-secondary px-2.5 py-1 text-xs transition-colors hover:bg-accent"
              >
                <Wifi size={10} className="text-success" />
                <span className="font-medium">
                  {selectedProvider ? selectedProvider.name : 'Select provider'}
                </span>
                {selectedProvider && (
                  <span className="text-muted-foreground">
                    ({selectedProvider.default_model})
                  </span>
                )}
                <ChevronDown size={12} className="text-muted-foreground" />
              </button>
              {showProviderMenu && (
                <div className="absolute left-0 top-full z-50 mt-1 min-w-[240px] rounded-lg border border-border bg-popover py-1 shadow-lg">
                  {providers.map((p) => (
                    <button
                      key={p.id}
                      type="button"
                      onClick={() => {
                        setSelectedProviderId(p.id)
                        setShowProviderMenu(false)
                      }}
                      className={cn(
                        'flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-accent',
                        p.id === selectedProviderId && 'bg-accent/50'
                      )}
                    >
                      <div className="flex flex-1 flex-col">
                        <span className="font-medium">{p.name}</span>
                        <span className="text-xs text-muted-foreground">
                          {p.provider_type} &middot; {p.default_model}
                          {p.supports_vision === false && ' · text only'}
                        </span>
                      </div>
                      {p.is_default_generation && (
                        <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
                          Default
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* No providers warning */}
        {providers.length === 0 && !isConnecting && (
          <div className="mb-2 rounded-lg border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-warning">
            No AI providers configured. Ask an admin to add one in AI Providers settings.
          </div>
        )}

        {/* Pending attachments */}
        {pending.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {pending.map((p) => (
              <AttachmentThumb
                key={p.key}
                att={p}
                status={p.status}
                error={p.error}
                note={p.note}
                onRemove={() => removePending(p.key)}
              />
            ))}
          </div>
        )}
        {(attachError || hasFailed || modelRejectsImages) && (
          <div className="mb-2 flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <div className="space-y-0.5">
              {attachError && <p>{attachError}</p>}
              {hasFailed && (
                <p>
                  Upload failed for: {pending.filter((p) => p.status === 'error').map((p) => `${p.name} (${p.error})`).join('; ')}.
                  Remove it to send.
                </p>
              )}
              {modelRejectsImages && selectedProvider && (
                <p>
                  {selectedProvider.name} ({selectedProvider.default_model}) is a text-only model and can't
                  read images or PDFs. Pick a vision-capable provider above, or remove those attachments.
                </p>
              )}
            </div>
          </div>
        )}

        <form onSubmit={handleSubmit} className="relative">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={onPaste}
            placeholder={isStreaming
              ? 'AI is generating...'
              : 'Describe what you want to build… (paste or drop screenshots, images, PDFs)'}
            disabled={isStreaming}
            rows={3}
            className={cn(
              'w-full resize-none rounded-xl border border-input bg-secondary px-4 py-3 pb-10 pr-12 text-sm',
              'placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring',
              'disabled:opacity-50'
            )}
          />
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={ACCEPT_ATTR}
            className="hidden"
            onChange={(e) => {
              void addFiles(Array.from(e.target.files ?? []))
              e.target.value = ''
            }}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={isStreaming}
            title="Attach screenshots, images, PDFs or text files (or paste / drag & drop)"
            aria-label="Attach files"
            className="absolute bottom-3 left-3 rounded-lg p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
          >
            <Paperclip size={16} />
          </button>
          <button
            type="submit"
            disabled={!canSend}
            className={cn(
              'absolute bottom-3 right-3 rounded-lg p-2 transition-colors',
              canSend
                ? 'bg-primary text-primary-foreground hover:bg-primary/90'
                : 'text-muted-foreground'
            )}
          >
            {isStreaming || uploading ? (
              <Loader2 size={18} className="animate-spin" />
            ) : (
              <Send size={18} />
            )}
          </button>
        </form>
      </div>
    </div>
  )
}
