import { useState, useRef, useEffect, type FormEvent } from 'react'
import { Send, Loader2, Sparkles, Wifi, WifiOff, ChevronDown } from 'lucide-react'
import { useChatStore } from '@/stores/chatStore'
import { apiClient } from '@/api/client'
import { MessageBubble } from './MessageBubble'
import { VerifyStatusBar } from './VerifyStatusBar'
import { cn } from '@/lib/utils'

interface AIProviderOption {
  id: string
  name: string
  provider_type: string
  default_model: string
  is_default_generation: boolean
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

export function ChatPanel({ appId }: ChatPanelProps) {
  const [input, setInput] = useState('')
  const [providers, setProviders] = useState<AIProviderOption[]>([])
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(null)
  const [showProviderMenu, setShowProviderMenu] = useState(false)
  const [templates, setTemplates] = useState<PromptTemplate[]>([])
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const providerRef = useRef<HTMLDivElement>(null)
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

  // Load available providers
  useEffect(() => {
    apiClient.get<AIProviderOption[]>('/ai/providers').then((data) => {
      setProviders(data)
      // Auto-select default
      const defaultProvider = data.find((p) => p.is_default_generation)
      if (defaultProvider) {
        setSelectedProviderId(defaultProvider.id)
      } else if (data.length > 0) {
        setSelectedProviderId(data[0].id)
      }
    }).catch(() => {
      // No providers configured
    })
  }, [])

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

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (!input.trim() || isStreaming) return
    sendMessage(appId, input.trim(), selectedProviderId)
    setInput('')
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit(e)
    }
  }

  return (
    <div className="flex h-full flex-col">
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
              You can iterate by chatting to refine the app.
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

        <form onSubmit={handleSubmit} className="relative">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={isStreaming ? 'AI is generating...' : 'Describe what you want to build...'}
            disabled={isStreaming}
            rows={3}
            className={cn(
              'w-full resize-none rounded-xl border border-input bg-secondary px-4 py-3 pr-12 text-sm',
              'placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring',
              'disabled:opacity-50'
            )}
          />
          <button
            type="submit"
            disabled={!input.trim() || isStreaming}
            className={cn(
              'absolute bottom-3 right-3 rounded-lg p-2 transition-colors',
              input.trim() && !isStreaming
                ? 'bg-primary text-primary-foreground hover:bg-primary/90'
                : 'text-muted-foreground'
            )}
          >
            {isStreaming ? (
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
