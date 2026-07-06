/**
 * AIToggleProvider — Wraps the app to enable AI Toggle functionality.
 *
 * When AI Toggle is enabled for an app, this provider:
 * 1. Shows a floating chat button in the corner
 * 2. Collects registered data sources and actions
 * 3. Sends context to the platform's AI Toggle backend
 * 4. Displays AI responses and executes action commands
 *
 * The app template mounts this once at the root (like BugReportButton), and
 * it auto-decides whether to show the assistant from the app's widget config
 * (the builder's AI toggle) — so it is safe to leave mounted on every app.
 * Pass `enabled` only to override that auto-detection:
 *
 *   <AIToggleProvider>                  // follows the platform AI toggle
 *   <AIToggleProvider enabled>          // always show (e.g. dev outside AIHub)
 *   <AIToggleProvider enabled={false}>  // never show
 */

import { useState, useEffect, useCallback, type ReactNode } from 'react'
import { getDataSources } from './useAIDataSource'
import { getActions, executeAction } from './useAIAction'
import { SESSION_EXPIRED_MESSAGE, hasSessionToken, sessionExpiredError } from './session'

// Same base-URL contract as every other SDK module: empty = same-origin
// (preview/viewer behind the runtime proxy); deployed apps get the platform
// origin baked in via VITE_AIHUB_BASE_URL. This module used to hard-code a
// relative '/api/...' URL, which 404'd on every deployed app.
const AIHUB_BASE: string =
  ((import.meta as any).env?.VITE_AIHUB_BASE_URL as string | undefined) || ''

interface WidgetConfig {
  ai_toggle_enabled?: boolean
}

interface AIToggleProviderProps {
  children: ReactNode
  /** Override auto-detection: true = always show the assistant, false =
   *  never. Leave unset to follow the app's widget config (the AI toggle
   *  in the builder top bar). */
  enabled?: boolean
}

interface Message {
  role: 'user' | 'assistant'
  content: string
}

export function AIToggleProvider({ children, enabled }: AIToggleProviderProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [configEnabled, setConfigEnabled] = useState(false)

  // Same self-gating contract as BugReportButton: unless the mount overrides
  // via `enabled`, ask the platform whether the AI toggle is on for this app.
  useEffect(() => {
    if (enabled !== undefined) return
    const appId =
      (typeof window !== 'undefined' && (window as any).__AIHUB_APP_ID__) ||
      document.querySelector('meta[name="aihub-app-id"]')?.getAttribute('content')
    if (!appId) {
      setConfigEnabled(false)
      return
    }
    fetch(`${AIHUB_BASE}/api/apps/${appId}/widget-config`)
      .then((r) => (r.ok ? (r.json() as Promise<WidgetConfig>) : null))
      .then((cfg) => setConfigEnabled(!!cfg?.ai_toggle_enabled))
      .catch(() => setConfigEnabled(false))
  }, [enabled])

  // Listen for programmatic chat messages
  useEffect(() => {
    const handleChat = (e: Event) => {
      const { message } = (e as CustomEvent).detail
      sendMessage(message)
    }
    window.addEventListener('aihub:ai-chat-message', handleChat)
    return () => window.removeEventListener('aihub:ai-chat-message', handleChat)
  }, [])

  const sendMessage = useCallback(async (text: string) => {
    if (!text.trim()) return

    const userMessage: Message = { role: 'user', content: text }
    setMessages((prev) => [...prev, userMessage])
    setInput('')
    setIsLoading(true)

    try {
      const appId = window.__AIHUB_APP_ID__
      const dataSources = getDataSources()
      const actions = getActions()

      const token = window.__AIHUB_TOKEN__
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      if (token) headers['Authorization'] = `Bearer ${token}`

      const response = await fetch(`${AIHUB_BASE}/api/ai-toggle/${appId}/chat`, {
        method: 'POST',
        headers,
        credentials: 'include',
        body: JSON.stringify({
          message: text,
          context: {
            dataSources: Object.entries(dataSources).map(([name, ds]) => ({
              name,
              columns: ds.columns,
              description: ds.description,
              rowCount: ds.data.length,
              sampleRows: ds.data.slice(0, 3),
            })),
            availableActions: actions,
          },
        }),
      })

      // 401 with a token attached = the injected session token expired —
      // retrying can never succeed, so say the truth ("reload") instead of
      // the generic "try again" below. Token-less 401s keep the generic path.
      if (response.status === 401 && hasSessionToken()) throw sessionExpiredError()
      if (!response.ok) throw new Error('Failed to get AI response')

      const result = await response.json()
      const assistantMessage: Message = { role: 'assistant', content: result.response }
      setMessages((prev) => [...prev, assistantMessage])

      // Execute any action commands from the AI
      if (result.actions) {
        for (const action of result.actions) {
          executeAction(action.name, action.params)
        }
      }

      // Dispatch response event for useAIChat
      window.dispatchEvent(
        new CustomEvent('aihub:ai-chat-response', {
          detail: { response: result.response },
        })
      )
    } catch (err) {
      const content =
        err instanceof Error && err.message === SESSION_EXPIRED_MESSAGE
          ? SESSION_EXPIRED_MESSAGE
          : 'Sorry, I encountered an error. Please try again.'
      setMessages((prev) => [...prev, { role: 'assistant', content }])
    } finally {
      setIsLoading(false)
    }
  }, [])

  if (!(enabled ?? configEnabled)) return <>{children}</>

  return (
    <>
      {children}

      {/* Floating chat button */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        style={{
          position: 'fixed',
          bottom: '24px',
          right: '24px',
          width: '48px',
          height: '48px',
          borderRadius: '50%',
          background: '#3b82f6',
          color: 'white',
          border: 'none',
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: '20px',
          boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
          zIndex: 9999,
        }}
        title="AI Assistant"
      >
        {isOpen ? '\u00D7' : '\u2728'}
      </button>

      {/* Chat panel */}
      {isOpen && (
        <div
          style={{
            position: 'fixed',
            bottom: '84px',
            right: '24px',
            width: '380px',
            height: '480px',
            borderRadius: '16px',
            background: '#18181b',
            border: '1px solid #27272a',
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
            zIndex: 9998,
            boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
          }}
        >
          {/* Header */}
          <div
            style={{
              padding: '12px 16px',
              borderBottom: '1px solid #27272a',
              fontSize: '13px',
              fontWeight: 600,
              color: '#fafafa',
            }}
          >
            AI Assistant
          </div>

          {/* Messages */}
          <div style={{ flex: 1, overflow: 'auto', padding: '12px' }}>
            {messages.length === 0 && (
              <p style={{ color: '#71717a', fontSize: '12px', textAlign: 'center', marginTop: '40px' }}>
                Ask me anything about the data on screen
              </p>
            )}
            {messages.map((msg, i) => (
              <div
                key={i}
                style={{
                  marginBottom: '8px',
                  textAlign: msg.role === 'user' ? 'right' : 'left',
                }}
              >
                <span
                  style={{
                    display: 'inline-block',
                    padding: '8px 12px',
                    borderRadius: '12px',
                    fontSize: '12px',
                    maxWidth: '85%',
                    background: msg.role === 'user' ? '#3b82f6' : '#27272a',
                    color: '#fafafa',
                  }}
                >
                  {msg.content}
                </span>
              </div>
            ))}
            {isLoading && (
              <div style={{ textAlign: 'left', marginBottom: '8px' }}>
                <span
                  style={{
                    display: 'inline-block',
                    padding: '8px 12px',
                    borderRadius: '12px',
                    fontSize: '12px',
                    background: '#27272a',
                    color: '#71717a',
                  }}
                >
                  Thinking...
                </span>
              </div>
            )}
          </div>

          {/* Input */}
          <div style={{ padding: '12px', borderTop: '1px solid #27272a' }}>
            <div style={{ display: 'flex', gap: '8px' }}>
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    sendMessage(input)
                  }
                }}
                placeholder="Ask about the data..."
                style={{
                  flex: 1,
                  padding: '8px 12px',
                  borderRadius: '8px',
                  border: '1px solid #3f3f46',
                  background: '#09090b',
                  color: '#fafafa',
                  fontSize: '12px',
                  outline: 'none',
                }}
              />
              <button
                onClick={() => sendMessage(input)}
                disabled={!input.trim() || isLoading}
                style={{
                  padding: '8px 16px',
                  borderRadius: '8px',
                  background: '#3b82f6',
                  color: 'white',
                  border: 'none',
                  fontSize: '12px',
                  cursor: 'pointer',
                  opacity: !input.trim() || isLoading ? 0.5 : 1,
                }}
              >
                Send
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
