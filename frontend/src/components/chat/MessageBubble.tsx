import { useState } from 'react'
import { User, Bot, AlertCircle, MapPin, ChevronRight, ChevronDown, Code2 } from 'lucide-react'
import type { ChatMessage, CodeRef } from '@/types'
import { cn } from '@/lib/utils'
import { useChatStore } from '@/stores/chatStore'
import { CodeBlock } from './CodeBlock'

// An inline-code token that looks like an app file path + optional :line or :line-line.
// Strict (must start `src/`, must have an extension) so ordinary inline code like
// `useDataset` never becomes a jump button.
const CODE_REF_RE = /^(src\/[\w./-]+\.[A-Za-z0-9]+)(?::(\d+)(?:-(\d+))?)?$/

function parseCodeRef(text: string): { path: string; startLine: number | null; endLine: number | null } | null {
  const m = CODE_REF_RE.exec(text.trim())
  if (!m) return null
  const start = m[2] ? parseInt(m[2], 10) : null
  const end = m[3] ? parseInt(m[3], 10) : start
  return { path: m[1], startLine: start, endLine: end }
}

interface MessageBubbleProps {
  message: ChatMessage
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user'
  const isError = message.role === 'system'

  if (isError) {
    return (
      <div className="flex items-start gap-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-destructive/10">
          <AlertCircle size={16} className="text-destructive" />
        </div>
        <div className="rounded-xl bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {message.content}
        </div>
      </div>
    )
  }

  return (
    <div className={cn('flex items-start gap-3', isUser && 'flex-row-reverse')}>
      <div
        className={cn(
          'flex h-8 w-8 shrink-0 items-center justify-center rounded-lg',
          isUser ? 'bg-primary/10' : 'bg-accent'
        )}
      >
        {isUser ? (
          <User size={16} className="text-primary" />
        ) : (
          <Bot size={16} className="text-foreground" />
        )}
      </div>
      <div
        className={cn(
          'max-w-[85%] rounded-xl px-4 py-3 text-sm',
          isUser ? 'bg-primary text-primary-foreground' : 'bg-accent text-foreground'
        )}
      >
        <FormattedContent content={message.content} />
        {!isUser && message.codeRefs && message.codeRefs.length > 0 && (
          <CodeRefChips refs={message.codeRefs} />
        )}
      </div>
    </div>
  )
}

// "Jump to code" chips rendered under an assistant message that included [[jump:...]]
// directives. Clicking one opens the Code panel and highlights the lines.
function CodeRefChips({ refs }: { refs: CodeRef[] }) {
  const requestCodeNav = useChatStore((s) => s.requestCodeNav)
  return (
    <div className="mt-2 flex flex-wrap gap-1.5 border-t border-border/50 pt-2">
      {refs.map((r, i) => {
        const name = r.path.split('/').pop()
        const lines = r.start
          ? r.end && r.end !== r.start
            ? `:${r.start}-${r.end}`
            : `:${r.start}`
          : ''
        return (
          <button
            key={i}
            type="button"
            onClick={() => requestCodeNav({ path: r.path, startLine: r.start, endLine: r.end })}
            className="flex items-center gap-1 rounded-md bg-primary/10 px-2 py-1 text-xs font-medium text-primary transition-colors hover:bg-primary/20"
            title={`Jump to ${r.path}${lines}`}
          >
            <MapPin size={11} />
            {name}
            {lines}
          </button>
        )
      })}
    </div>
  )
}

function FormattedContent({ content }: { content: string }) {
  if (!content) {
    return <span className="animate-pulse text-muted-foreground">Thinking...</span>
  }

  // Split the content into "prose" segments and "fenced code block" segments
  // BEFORE we do anything else. Otherwise the line-by-line + InlineFormatted
  // path mangles ``` fences (a single ``` becomes a one-char inline code).
  const segments = splitFencedCodeBlocks(content)
  const elements: React.ReactNode[] = []

  segments.forEach((seg, segIdx) => {
    if (seg.kind === 'code') {
      elements.push(
        <CollapsibleCode key={`code-${segIdx}`} language={seg.language || 'plaintext'} code={seg.code} />
      )
      return
    }
    // Prose segment — render line-by-line with inline formatting
    const lines = seg.text.split('\n')
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i]
      if (!line.trim()) {
        // Collapse runs of empty lines into a single small gap, like the old code did
        if (i > 0 && lines[i - 1]?.trim()) {
          elements.push(<div key={`gap-${segIdx}-${i}`} className="h-2" />)
        }
        continue
      }
      elements.push(
        <div key={`line-${segIdx}-${i}`} className="leading-relaxed">
          <InlineFormatted text={line} />
        </div>
      )
    }
  })

  return <div>{elements}</div>
}

// Fenced code blocks render collapsed by default — a one-click chip instead of a
// full panel. The live builder never shows code in the bubble (the backend
// suppresses file content mid-stream and the `done` handler swaps in a summary),
// so an unexpanded fenced block only appears when a conversation is reloaded from
// history. Collapsing keeps the reloaded view as uncluttered as the live one while
// leaving the code one click away.
//
// Generated file blocks start with a `// FILE: <path>` marker (the same convention
// the backend parser keys on — see backend/src/ai/code_parser.py). We lift that
// path onto the chip as its label and strip the marker line from the displayed
// body, so the expanded code matches the real on-disk file (the backend strips the
// marker too). Blocks without a marker fall back to a generic label.
function CollapsibleCode({ language, code }: { language: string; code: string }) {
  const [expanded, setExpanded] = useState(false)

  const fileMatch = code.match(/^\s*\/\/\s*FILE:\s*(\S+)[ \t]*\r?\n?/)
  const path = fileMatch ? fileMatch[1] : null
  const body = fileMatch ? code.slice(fileMatch[0].length) : code

  const lineCount = body.trim() ? body.trimEnd().split('\n').length : 0
  const lines = lineCount ? `${lineCount} line${lineCount === 1 ? '' : 's'}` : ''
  const fileName = path ? path.split('/').pop() : null
  const label = fileName ?? (expanded ? 'Hide code' : 'View code')
  const detail = fileName ? lines : [language, lines].filter(Boolean).join(' · ')

  return (
    <div className="my-1">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        title={path ?? undefined}
        className="flex w-full items-center gap-2 rounded-lg border border-border bg-background/60 px-3 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:bg-background hover:text-foreground"
      >
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <Code2 size={14} />
        <span className="font-medium text-foreground">{label}</span>
        {detail && <span>{detail}</span>}
      </button>
      {expanded && <CodeBlock language={language} code={body} />}
    </div>
  )
}

interface CodeSegment { kind: 'code'; language: string; code: string }
interface ProseSegment { kind: 'prose'; text: string }
type ContentSegment = CodeSegment | ProseSegment

/**
 * Split markdown-ish content into prose vs ```fenced code``` runs.
 *
 * Recognises:
 *   ```lang\n…\n```   - properly closed (rendered as <CodeBlock>)
 *   ```lang\n…<EOF>    - unclosed (also rendered as <CodeBlock>) — happens
 *                       mid-stream before the closing fence has arrived.
 *
 * This MUST run before any inline-backtick handling, otherwise a bare ```
 * fence character on its own gets misread as inline-code with the middle
 * backtick as its content.
 */
function splitFencedCodeBlocks(content: string): ContentSegment[] {
  const segments: ContentSegment[] = []
  // Match: 3+ backticks, optional language, newline, lazy capture, then closing
  // fence OR end of string. Treat both as one block.
  const fenceRe = /```(\w*)\s*\n([\s\S]*?)(?:\n```|$)/g
  let lastEnd = 0
  let m: RegExpExecArray | null
  while ((m = fenceRe.exec(content)) !== null) {
    if (m.index > lastEnd) {
      segments.push({ kind: 'prose', text: content.slice(lastEnd, m.index) })
    }
    segments.push({ kind: 'code', language: m[1] || '', code: m[2] || '' })
    lastEnd = m.index + m[0].length
  }
  if (lastEnd < content.length) {
    segments.push({ kind: 'prose', text: content.slice(lastEnd) })
  }
  return segments
}

function InlineFormatted({ text }: { text: string }) {
  const requestCodeNav = useChatStore((s) => s.requestCodeNav)
  // Process inline formatting: **bold**, `code` (with `src/…:line` codes made clickable).
  // The `code` regex requires NON-backtick content between the backticks, which
  // prevents fenced-fence runs (```) from being misread as inline code. Fenced
  // blocks are already pulled out one level up in splitFencedCodeBlocks().
  const parts = text.split(/(\*\*.*?\*\*|`[^`\n]+?`)/g)

  return (
    <>
      {parts.map((part, i) => {
        if (part.startsWith('**') && part.endsWith('**')) {
          return (
            <strong key={i} className="font-semibold">
              {part.slice(2, -2)}
            </strong>
          )
        }
        // Inline code must be exactly: ` + 1+ non-backtick chars + `
        // (not multiple backticks in a row — those are fence remnants)
        if (
          part.length >= 3
          && part.startsWith('`')
          && part.endsWith('`')
          && !part.startsWith('``')
          && !part.endsWith('``')
        ) {
          const inner = part.slice(1, -1)
          // If it looks like an app file path (+ optional :line), make it a jump button.
          const ref = parseCodeRef(inner)
          if (ref) {
            return (
              <button
                key={i}
                type="button"
                onClick={() => requestCodeNav(ref)}
                className="rounded bg-primary/10 px-1.5 py-0.5 text-xs font-mono text-primary underline-offset-2 transition-colors hover:bg-primary/20 hover:underline"
                title={`Jump to ${ref.path}`}
              >
                {inner}
              </button>
            )
          }
          return (
            <code
              key={i}
              className="rounded bg-background/50 px-1.5 py-0.5 text-xs font-mono"
            >
              {inner}
            </code>
          )
        }
        return <span key={i}>{part}</span>
      })}
    </>
  )
}
