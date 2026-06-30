import { useEffect, useRef, useState, type FormEvent } from 'react'
import {
  GripVertical, Minimize2, Maximize2, X, Eye, Send, Loader2, Sparkles, MapPin, WifiOff,
} from 'lucide-react'
import { useChatStore } from '@/stores/chatStore'
import { MessageBubble } from '@/components/chat/MessageBubble'
import { cn } from '@/lib/utils'

interface CollabOverlayProps {
  appId: string
  onClose: () => void
}

// --- persisted overlay prefs (per developer) ----------------------------------------
const POS_KEY = 'aihub.collab.pos'
const OPACITY_KEY = 'aihub.collab.opacity'
const MIN_KEY = 'aihub.collab.min'

function readPos(): { left: number; top: number } | null {
  try {
    const v = localStorage.getItem(POS_KEY)
    if (!v) return null
    const p = JSON.parse(v)
    return typeof p?.left === 'number' && typeof p?.top === 'number' ? p : null
  } catch {
    return null
  }
}
function readNum(key: string, dflt: number): number {
  try {
    const v = parseFloat(localStorage.getItem(key) || '')
    return Number.isFinite(v) ? v : dflt
  } catch {
    return dflt
  }
}
function readBoolLS(key: string, dflt: boolean): boolean {
  try {
    const v = localStorage.getItem(key)
    return v === null ? dflt : v === '1'
  } catch {
    return dflt
  }
}

function clampToParent(card: HTMLElement, left: number, top: number) {
  const parent = card.offsetParent as HTMLElement | null
  const pw = parent?.clientWidth ?? window.innerWidth
  const ph = parent?.clientHeight ?? window.innerHeight
  return {
    left: Math.max(0, Math.min(left, pw - card.offsetWidth)),
    top: Math.max(0, Math.min(top, ph - card.offsetHeight)),
  }
}

/**
 * Floating "window" chat docked over the Code editor. Drag it anywhere (pinned position
 * persists), minimize it while reading, and dial transparency so the code shows through.
 * It drives the SAME conversation as the Chat tab and sends the AI the file/selection the
 * user is looking at (chatStore.editorContext).
 */
export function CollabOverlay({ appId, onClose }: CollabOverlayProps) {
  const messages = useChatStore((s) => s.messages)
  const isStreaming = useChatStore((s) => s.isStreaming)
  const isConnected = useChatStore((s) => s.isConnected)
  const sendMessage = useChatStore((s) => s.sendMessage)
  const editorContext = useChatStore((s) => s.editorContext)

  const [input, setInput] = useState('')
  const [pos, setPos] = useState<{ left: number; top: number } | null>(() => readPos())
  const [opacity, setOpacity] = useState(() => readNum(OPACITY_KEY, 1))
  const [minimized, setMinimized] = useState(() => readBoolLS(MIN_KEY, false))
  const [peek, setPeek] = useState(false) // force full opacity while hovered/focused

  const cardRef = useRef<HTMLDivElement>(null)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, minimized])

  const setOpacityPersist = (v: number) => {
    setOpacity(v)
    try { localStorage.setItem(OPACITY_KEY, String(v)) } catch { /* ignore */ }
  }
  const toggleMin = () => {
    setMinimized((m) => {
      const next = !m
      try { localStorage.setItem(MIN_KEY, next ? '1' : '0') } catch { /* ignore */ }
      return next
    })
  }

  const onDragPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return
    const card = cardRef.current
    if (!card) return
    e.preventDefault()
    const startX = e.clientX
    const startY = e.clientY
    const startLeft = card.offsetLeft
    const startTop = card.offsetTop
    const onMove = (ev: PointerEvent) => {
      setPos(clampToParent(card, startLeft + (ev.clientX - startX), startTop + (ev.clientY - startY)))
    }
    const onUp = () => {
      document.removeEventListener('pointermove', onMove)
      document.removeEventListener('pointerup', onUp)
      document.body.style.userSelect = ''
      setPos((p) => {
        if (p) { try { localStorage.setItem(POS_KEY, JSON.stringify(p)) } catch { /* ignore */ } }
        return p
      })
    }
    document.addEventListener('pointermove', onMove)
    document.addEventListener('pointerup', onUp)
    document.body.style.userSelect = 'none'
  }

  const handleSend = (e: FormEvent) => {
    e.preventDefault()
    if (!input.trim() || isStreaming) return
    sendMessage(appId, input.trim(), null, editorContext)
    setInput('')
  }

  // Position: explicit left/top once dragged, otherwise a sensible default near the top-right.
  const positionStyle: React.CSSProperties = pos
    ? { left: pos.left, top: pos.top }
    : { right: 24, top: 16 }

  const ctxName = editorContext?.path?.split('/').pop()
  const hasSel = !!editorContext?.selectionText
  const selRange =
    hasSel && editorContext?.selStartLine
      ? editorContext.selEndLine && editorContext.selEndLine !== editorContext.selStartLine
        ? `${editorContext.selStartLine}–${editorContext.selEndLine}`
        : `${editorContext.selStartLine}`
      : null

  return (
    <div
      ref={cardRef}
      onMouseEnter={() => setPeek(true)}
      onMouseLeave={() => setPeek(false)}
      style={{ ...positionStyle, opacity: peek ? 1 : opacity }}
      className={cn(
        'absolute z-30 flex w-[380px] flex-col overflow-hidden rounded-xl border border-border',
        'bg-card shadow-2xl transition-opacity',
        minimized ? 'h-auto' : 'max-h-[78%]'
      )}
    >
      {/* Header / drag handle */}
      <div
        onPointerDown={onDragPointerDown}
        className="flex cursor-grab items-center gap-2 border-b border-border bg-muted/60 px-3 py-2 active:cursor-grabbing"
      >
        <GripVertical size={14} className="text-muted-foreground" />
        <Sparkles size={13} className="text-primary" />
        <span className="text-xs font-semibold">Collaborate</span>
        <div className="ml-auto flex items-center gap-1.5" onPointerDown={(e) => e.stopPropagation()}>
          {/* Transparency */}
          <div className="flex items-center gap-1" title="Overlay transparency — see the code through it">
            <Eye size={13} className="text-muted-foreground" />
            <input
              type="range"
              min={0.4}
              max={1}
              step={0.05}
              value={opacity}
              onChange={(e) => setOpacityPersist(parseFloat(e.target.value))}
              className="h-1 w-16 cursor-pointer accent-primary"
            />
          </div>
          <button
            onClick={toggleMin}
            className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            title={minimized ? 'Expand' : 'Minimize'}
          >
            {minimized ? <Maximize2 size={13} /> : <Minimize2 size={13} />}
          </button>
          <button
            onClick={onClose}
            className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            title="Close"
          >
            <X size={13} />
          </button>
        </div>
      </div>

      {!minimized && (
        <>
          {/* Context chip — what the AI will be told you're looking at */}
          <div className="flex items-center gap-1.5 border-b border-border/60 px-3 py-1.5 text-[11px] text-muted-foreground">
            <MapPin size={11} className="shrink-0 text-primary/70" />
            {ctxName ? (
              <span className="truncate">
                Looking at <span className="font-mono text-foreground">{ctxName}</span>
                {selRange && <span className="text-primary"> · lines {selRange} selected</span>}
              </span>
            ) : (
              <span className="truncate">Open a file in the Code panel to share what you're viewing.</span>
            )}
          </div>

          {/* Transcript */}
          <div className="flex-1 space-y-3 overflow-y-auto p-3">
            {messages.length === 0 ? (
              <p className="py-6 text-center text-xs text-muted-foreground">
                Ask about the code you're viewing. Highlight a snippet first to focus the AI on it.
              </p>
            ) : (
              messages.map((m) => <MessageBubble key={m.id} message={m} />)
            )}
            {isStreaming && (
              <div className="flex items-center gap-2 px-1 text-[11px] text-muted-foreground">
                <Loader2 size={11} className="animate-spin text-primary" />
                Working…
              </div>
            )}
            <div ref={endRef} />
          </div>

          {/* Input */}
          <form onSubmit={handleSend} className="border-t border-border p-2">
            {!isConnected && (
              <div className="mb-1 flex items-center gap-1 px-1 text-[11px] text-warning">
                <WifiOff size={11} /> Reconnecting…
              </div>
            )}
            <div className="relative">
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onFocus={() => setPeek(true)}
                onBlur={() => setPeek(false)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    handleSend(e)
                  }
                }}
                placeholder={hasSel ? 'Ask about the selected code…' : 'Ask about this file…'}
                disabled={isStreaming}
                rows={2}
                className="w-full resize-none rounded-lg border border-input bg-secondary px-3 py-2 pr-10 text-xs placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
              />
              <button
                type="submit"
                disabled={!input.trim() || isStreaming}
                className={cn(
                  'absolute bottom-2 right-2 rounded-md p-1.5 transition-colors',
                  input.trim() && !isStreaming
                    ? 'bg-primary text-primary-foreground hover:bg-primary/90'
                    : 'text-muted-foreground'
                )}
              >
                {isStreaming ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
              </button>
            </div>
          </form>
        </>
      )}
    </div>
  )
}
