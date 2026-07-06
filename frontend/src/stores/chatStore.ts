import { create } from 'zustand'
import { apiClient } from '@/api/client'
import type { ChatMessage, CodeRef, EditorContext } from '@/types'

interface FileChange {
  path: string
  action: string
}

// Intent to open a file in the Code panel and reveal/highlight a line range. The builder
// page watches `codeNav.token` and navigates on change (the token makes re-jumping to the
// same range re-fire). startLine/endLine null = just open the file.
export interface CodeNav {
  path: string
  startLine: number | null
  endLine: number | null
  token: number
}

// Live code-streaming view: the file the AI is currently writing, accumulated from
// `code_stream` events. Ephemeral — reset on each new send.
export interface LiveCode {
  path: string
  content: string
}

// --- Per-developer builder prefs, persisted in localStorage --------------------------
const LIVE_CODE_KEY = 'aihub.builder.liveCode'
const AUTO_JUMP_KEY = 'aihub.builder.autoJump'

function readBool(key: string, dflt: boolean): boolean {
  try {
    const v = localStorage.getItem(key)
    return v === null ? dflt : v === '1'
  } catch {
    return dflt
  }
}

function writeBool(key: string, v: boolean): void {
  try {
    localStorage.setItem(key, v ? '1' : '0')
  } catch {
    /* ignore (private mode / disabled storage) */
  }
}

let navToken = 0

// Conservative detector for "take me to the code" intent. Auto-jump only fires when the
// user's message reads like an explicit navigation request — a plain "add/change/fix X"
// must NOT yank them into the Code panel. When unsure, return false (the jump chips are
// always there to click). Used to gate auto-navigation in the `done` handler.
const NAV_INTENT_RE = new RegExp(
  [
    /\bshow me\b/,
    /\btake me (to|there)\b/,
    /\bjump to\b/,
    /\bgo to\b/,
    /\bnavigate (to|me)\b/,
    /\bwhere(?:'s| is| are| can i (?:find|see))\b/,
    /\b(?:find|point me to|take me to|bring me to|locate|reveal)\b[^.?!]*\b(code|file|component|hook|function|line|method|class|where)\b/,
    /\bopen (?:the |up )?[\w./-]*\.(tsx?|jsx?|css)\b/,
    /\b(let me see|can i see|i want to see)\b/,
  ].map((r) => r.source).join('|'),
  'i',
)

export function isNavigationRequest(message: string): boolean {
  return NAV_INTENT_RE.test(message || '')
}

// Mirrors VerifyError on the backend (backend/src/ai/verifier.py).
export interface VerifyError {
  stage: 'tsc' | 'build' | 'boot'
  file: string | null
  line: number | null
  column: number | null
  code: string | null
  message: string
}

export interface VerifyResult {
  stage_reached: string
  duration_seconds: number
  passed: boolean
  summary: string
  errors: VerifyError[]
}

// Streamed progress for the active turn's self-heal loop.
export interface VerifyProgress {
  iteration: number         // 0 = initial pass, 1+ = fix attempts
  max: number
  status: 'running' | 'iteration_done'
  passed?: boolean
  stage?: string
  summary?: string
  errors?: string[]         // short, one-line messages (full errors are in verifyResult)
  duration_seconds?: number
}

interface ChatState {
  messages: ChatMessage[]
  isStreaming: boolean
  isConnected: boolean
  isConnecting: boolean
  connectionError: string | null
  conversationId: string | null
  ws: WebSocket | null
  lastFilesChanged: FileChange[]

  // Verification status for the most recent turn — drives the live
  // "Verifying… / Found 2 errors, fixing… / ✓ Verified" panel and the
  // post-turn rollback affordance.
  verifyProgress: VerifyProgress | null
  verifyResult: VerifyResult | null
  rollbackAvailable: boolean
  currentAppId: string | null

  // Jump-to-code + live code-streaming.
  codeNav: CodeNav | null      // pending "open this file + highlight" intent
  liveCode: LiveCode | null    // file the AI is currently writing (live view)
  liveCodeEnabled: boolean     // watch the AI write code live (persisted pref)
  autoJumpEnabled: boolean     // auto-open+highlight code the AI references (persisted pref)
  lastSendWasNavRequest: boolean  // did the last sent message read as "take me to the code"?

  // In-code collaboration overlay.
  editorContext: EditorContext | null  // what the user is currently looking at in the Code panel
  turnFilePaths: string[]              // files the AI changed THIS turn (for live tab refresh)

  addMessage: (message: ChatMessage) => void
  appendToLastMessage: (text: string) => void
  replaceLastMessage: (content: string) => void
  setStreaming: (streaming: boolean) => void
  setConversationId: (id: string | null) => void
  clearMessages: () => void
  loadHistory: (messages: ChatMessage[], conversationId: string | null) => void
  connect: () => Promise<void>
  sendMessage: (appId: string, message: string, providerId?: string | null, editorContext?: EditorContext | null) => void
  rollbackDraft: () => Promise<{ ok: boolean; error?: string }>
  dismissVerifyResult: () => void
  disconnect: () => void

  requestCodeNav: (ref: { path: string; startLine?: number | null; endLine?: number | null }) => void
  setLiveCodeEnabled: (v: boolean) => void
  setAutoJumpEnabled: (v: boolean) => void
  setLastMessageCodeRefs: (refs: CodeRef[]) => void
  setEditorContext: (ctx: EditorContext | null) => void
}

let messageIdCounter = 0

// WebSocket auto-reconnect. A backend restart/deploy drops open sockets; without
// this the builder strands on "Not connected — refresh the page". Kept in module
// scope so timers/counters survive store updates and aren't React-observable.
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let reconnectAttempts = 0
let intentionalClose = false
const MAX_RECONNECT_ATTEMPTS = 12

export const useChatStore = create<ChatState>((set, get) => ({
  messages: [],
  isStreaming: false,
  isConnected: false,
  isConnecting: false,
  connectionError: null,
  conversationId: null,
  ws: null,
  lastFilesChanged: [],

  verifyProgress: null,
  verifyResult: null,
  rollbackAvailable: false,
  currentAppId: null,

  codeNav: null,
  liveCode: null,
  liveCodeEnabled: readBool(LIVE_CODE_KEY, false),
  autoJumpEnabled: readBool(AUTO_JUMP_KEY, true),
  lastSendWasNavRequest: false,
  editorContext: null,
  turnFilePaths: [],

  addMessage: (message) =>
    set((state) => ({ messages: [...state.messages, message] })),

  appendToLastMessage: (text) =>
    set((state) => {
      const msgs = [...state.messages]
      const last = msgs[msgs.length - 1]
      if (last && last.role === 'assistant') {
        msgs[msgs.length - 1] = { ...last, content: last.content + text }
      }
      return { messages: msgs }
    }),

  replaceLastMessage: (content) =>
    set((state) => {
      const msgs = [...state.messages]
      const last = msgs[msgs.length - 1]
      if (last && last.role === 'assistant') {
        msgs[msgs.length - 1] = { ...last, content }
      }
      return { messages: msgs }
    }),

  setStreaming: (streaming) => set({ isStreaming: streaming }),
  setConversationId: (id) => set({ conversationId: id }),
  clearMessages: () => set({
    messages: [], conversationId: null, lastFilesChanged: [],
    verifyProgress: null, verifyResult: null, rollbackAvailable: false,
  }),
  loadHistory: (messages, conversationId) => set({ messages, conversationId }),

  connect: async () => {
    // A fresh/explicit connect cancels any pending auto-reconnect attempt.
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
    intentionalClose = false

    const { ws } = get()
    if (ws && ws.readyState === WebSocket.OPEN) {
      set({ isConnected: true, isConnecting: false, connectionError: null })
      return
    }

    set({ isConnecting: true, connectionError: null })

    // Freshen the access token BEFORE authenticating the socket. Tokens can be
    // as short as 15 min; a reconnect after idle/sleep (or a token captured at
    // page load) would otherwise present a dead token and the backend answers
    // {"type":"error","data":"Invalid token"} + close — which used to spam the
    // chat with one red error bubble per retry. Any authenticated request does
    // it: apiClient transparently refreshes on 401 and retries.
    try {
      await apiClient.get('/auth/me')
    } catch {
      // Backend unreachable or genuinely signed out — try the socket anyway;
      // its own failure path handles retry/backoff.
    }
    const token = apiClient.getToken()

    return new Promise<void>((resolve, reject) => {
      // Tracks whether THIS socket got past the auth handshake — auth-phase
      // errors are connection plumbing (handled by the reconnect path), not
      // conversation content, so they must never become chat bubbles.
      let authenticated = false
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      // In dev, Vite's proxy can fail to forward this WebSocket upgrade to Chrome
      // (HMR connects, but the proxied /api WS upgrade hangs), so connect straight to
      // the backend — mirrors the preview iframe's import.meta.env.DEV host switch in
      // AppBuilderPage. In prod the SPA is same-origin, so window.location.host already
      // points at the backend and no proxy is involved.
      const wsHost = import.meta.env.DEV ? 'localhost:8800' : window.location.host
      const socket = new WebSocket(`${protocol}//${wsHost}/api/ai/chat`)

      socket.onopen = () => {
        socket.send(JSON.stringify({ token }))
      }

      socket.onmessage = (event) => {
        const data = JSON.parse(event.data)

        switch (data.type) {
          case 'authenticated':
            authenticated = true
            reconnectAttempts = 0
            set({ ws: socket, isConnected: true, isConnecting: false, connectionError: null })
            resolve()
            break

          case 'status':
            // Status update — show as assistant message if streaming hasn't started
            get().appendToLastMessage(data.data)
            break

          case 'text':
            get().appendToLastMessage(data.data)
            break

          case 'files': {
            // Files were generated — store them (for the summary) and accumulate the set of
            // paths changed this turn so the editor can refresh those open tabs live.
            const changed = (data.data || []) as FileChange[]
            set((state) => ({
              lastFilesChanged: changed,
              turnFilePaths: Array.from(new Set([...state.turnFilePaths, ...changed.map((f) => f.path)])),
            }))
            break
          }

          case 'code_stream': {
            // Live "watch the AI write" — accumulate the file currently being written.
            const d = data.data || {}
            if (d.event === 'file_start') {
              set({ liveCode: { path: d.path || '', content: '' } })
            } else if (d.event === 'delta') {
              set((state) => {
                const cur = state.liveCode
                if (!cur || cur.path !== d.path) {
                  // A new file started streaming (or a delta before file_start) — switch to it.
                  return { liveCode: { path: d.path || cur?.path || '', content: d.text || '' } }
                }
                return { liveCode: { path: cur.path, content: cur.content + (d.text || '') } }
              })
            }
            // file_end: keep the content shown until the next file/turn.
            break
          }

          case 'verifying':
            // Start of a verification pass (iteration 0 = initial, 1..N = fix attempts).
            set({
              verifyProgress: {
                iteration: data.data?.iteration ?? 0,
                max: data.data?.max ?? 0,
                status: 'running',
              },
            })
            break

          case 'verify_iteration':
            // Outcome of one verification pass.
            set({
              verifyProgress: {
                iteration: data.data?.iteration ?? 0,
                max: get().verifyProgress?.max ?? 0,
                status: 'iteration_done',
                passed: !!data.data?.passed,
                stage: data.data?.stage,
                summary: data.data?.summary,
                errors: data.data?.errors,
                duration_seconds: data.data?.duration_seconds,
              },
            })
            break

          case 'done': {
            const { description, files_changed, verify, rollback_available } = data.data || {}

            // Replace streamed text with clean description + file summary
            if (description || files_changed > 0) {
              let finalContent = description || ''
              if (files_changed > 0) {
                const fileList = get().lastFilesChanged
                if (fileList.length > 0) {
                  const fileNames = fileList.map((f: FileChange) => {
                    const name = f.path.split('/').pop()
                    return `\`${name}\``
                  }).join(', ')
                  finalContent += `\n\n**${files_changed} file${files_changed > 1 ? 's' : ''} generated:** ${fileNames}`
                  finalContent += '\n\nSwitch to the **Code** tab to view the files, or click **Preview** to see the app running.'
                }
              }
              if (finalContent.trim()) {
                get().replaceLastMessage(finalContent.trim())
              }
            }

            set({
              isStreaming: false,
              lastFilesChanged: [],
              verifyProgress: null,
              verifyResult: verify || null,
              rollbackAvailable: !!rollback_available,
            })
            if (data.data?.conversation_id) {
              set({ conversationId: data.data.conversation_id })
            }

            // Attach the AI's code pointers to the message (clickable chips) — ALWAYS.
            // Auto-navigation, however, only fires when the user's message actually asked to
            // be taken to code (e.g. "show me…"); a plain "add/change X" leaves you put.
            const codeRefs = (data.data?.code_refs || []) as CodeRef[]
            if (codeRefs.length > 0) {
              get().setLastMessageCodeRefs(codeRefs)
              if (get().autoJumpEnabled && get().lastSendWasNavRequest) {
                const r = codeRefs[0]
                get().requestCodeNav({ path: r.path, startLine: r.start, endLine: r.end })
              }
            }
            break
          }

          case 'auth_error':
            // Typed auth failure (v0.7.8 contract; closes with code 4401).
            // Never conversation content. Before the handshake it's plumbing —
            // onclose reconnects with a freshened token. After the handshake
            // it's TERMINAL (account deactivated mid-session): mark the close
            // intentional so onclose does NOT reconnect — otherwise ~12 doomed
            // retries fire and each wipes the explanatory banner, so the user
            // never learns why chat stopped.
            if (authenticated) {
              intentionalClose = true
              set({
                isConnecting: false,
                connectionError: data.data?.message || 'Your session is no longer valid.',
              })
            } else {
              set({ isConnecting: true, connectionError: null })
            }
            break

          case 'error':
            // Auth-phase rejections ("Invalid token", "Authentication
            // required") are NOT conversation content — the server closes the
            // socket and onclose reconnects with a freshened token. Rendering
            // them used to stack one red bubble per retry after the token
            // expired while the builder sat idle. (Kept for pre-v0.7.8
            // backends; new ones send the typed auth_error above.)
            if (!authenticated) {
              set({ isConnecting: true, connectionError: null })
              break
            }
            set({ isStreaming: false })
            get().addMessage({
              id: `error-${++messageIdCounter}`,
              role: 'system',
              content: `Error: ${data.data}`,
              timestamp: new Date().toISOString(),
            })
            break
        }
      }

      socket.onerror = () => {
        set({ isConnected: false, isConnecting: false, connectionError: 'WebSocket connection failed' })
        reject(new Error('WebSocket connection failed'))
      }

      socket.onclose = () => {
        set({ ws: null, isConnected: false })
        if (intentionalClose) {
          set({ isConnecting: false })
          return
        }
        // Unexpected drop (backend restart, network blip): reconnect with
        // exponential backoff so the builder recovers without a manual refresh.
        if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
          set({ isConnecting: false, connectionError: 'Lost connection to the AI service. Refresh to retry.' })
          return
        }
        const delay = Math.min(15000, 1000 * 2 ** reconnectAttempts)
        reconnectAttempts += 1
        set({ isConnecting: true, connectionError: null })
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null
          // No captured credential: connect() freshens the token itself. The
          // old `connect(token)` closure replayed the SAME token forever —
          // guaranteed-dead after 15 min of sitting idle.
          get().connect().catch(() => { /* onclose schedules the next retry */ })
        }, delay)
      }
    })
  },

  sendMessage: (appId: string, message: string, providerId?: string | null, editorContext?: EditorContext | null) => {
    const { ws, conversationId, isConnected } = get()

    if (!ws || ws.readyState !== WebSocket.OPEN || !isConnected) {
      get().addMessage({
        id: `error-${++messageIdCounter}`,
        role: 'system',
        content: 'Not connected to AI service. Please refresh the page and try again.',
        timestamp: new Date().toISOString(),
      })
      return
    }

    // Add user message to UI
    get().addMessage({
      id: `user-${++messageIdCounter}`,
      role: 'user',
      content: message,
      timestamp: new Date().toISOString(),
    })

    // Add empty assistant message for streaming
    get().addMessage({
      id: `assistant-${++messageIdCounter}`,
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
    })

    // Reset verify + live-code + per-turn state on a new send. Record whether this message
    // reads as a "take me to the code" request — the done handler uses it to gate auto-jump.
    set({
      isStreaming: true,
      currentAppId: appId,
      verifyProgress: null,
      verifyResult: null,
      rollbackAvailable: false,
      liveCode: null,
      turnFilePaths: [],
      lastSendWasNavRequest: isNavigationRequest(message),
    })

    ws.send(
      JSON.stringify({
        app_id: appId,
        message,
        conversation_id: conversationId,
        provider_id: providerId || undefined,
        // Ask the backend to stream code_stream events so the Live panel can show the
        // AI writing each file. Off by default; harmless when the panel is closed.
        live_code: get().liveCodeEnabled,
        // What the user is looking at in the editor (sent from the in-code overlay) so the
        // AI focuses on the exact file/selection on screen. Omitted from the normal Chat tab.
        editor_context: editorContext || undefined,
      })
    )
  },

  rollbackDraft: async () => {
    const appId = get().currentAppId
    if (!appId) return { ok: false, error: 'No active app' }
    try {
      await apiClient.post(`/apps/${appId}/rollback-draft`)
      // Roll the chat panel back to a clean state too.
      set({ verifyResult: null, rollbackAvailable: false })
      return { ok: true }
    } catch (e: any) {
      return { ok: false, error: e?.message || 'Rollback failed' }
    }
  },

  dismissVerifyResult: () => set({ verifyResult: null, rollbackAvailable: false }),

  disconnect: () => {
    // Explicit teardown (leaving the builder): stop auto-reconnect.
    intentionalClose = true
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
    reconnectAttempts = 0
    const { ws } = get()
    if (ws) {
      ws.close()
    }
    set({ ws: null, isConnected: false, isConnecting: false })
  },

  requestCodeNav: (ref) =>
    set({
      codeNav: {
        path: ref.path,
        startLine: ref.startLine ?? null,
        endLine: ref.endLine ?? ref.startLine ?? null,
        token: ++navToken,
      },
    }),

  setLiveCodeEnabled: (v) => {
    writeBool(LIVE_CODE_KEY, v)
    set({ liveCodeEnabled: v })
  },

  setAutoJumpEnabled: (v) => {
    writeBool(AUTO_JUMP_KEY, v)
    set({ autoJumpEnabled: v })
  },

  setLastMessageCodeRefs: (refs) =>
    set((state) => {
      const msgs = [...state.messages]
      const last = msgs[msgs.length - 1]
      if (last && last.role === 'assistant') {
        msgs[msgs.length - 1] = { ...last, codeRefs: refs }
      }
      return { messages: msgs }
    }),

  setEditorContext: (ctx) => set({ editorContext: ctx }),
}))
