import { useState } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { User, Bot, AlertCircle, MapPin, ChevronRight, ChevronDown, Code2 } from 'lucide-react'
import type { ChatMessage, CodeRef } from '@/types'
import { cn } from '@/lib/utils'
import { useChatStore } from '@/stores/chatStore'
import { CodeBlock } from './CodeBlock'
import { MermaidBlock } from './MermaidBlock'
import { MockupBlock } from './MockupBlock'

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
  // BEFORE handing prose to react-markdown. The custom splitter (not the
  // markdown parser) owns fences because it must also handle UNCLOSED fences
  // mid-stream, route mermaid/mockup/svg to visual renderers, and collapse
  // `// FILE:` blocks — none of which stock markdown gives us.
  const segments = splitFencedCodeBlocks(content)

  return (
    <div>
      {segments.map((seg, segIdx) => {
        if (seg.kind === 'code') {
          // Visual blocks render as visuals: diagrams and screen mockups are the
          // conversation's deliverable, not code to collapse.
          if (seg.language === 'mermaid') {
            return <MermaidBlock key={`code-${segIdx}`} code={seg.code} />
          }
          if (seg.language === 'mockup' || seg.language === 'svg') {
            return <MockupBlock key={`code-${segIdx}`} language={seg.language} code={seg.code} />
          }
          return (
            <CollapsibleCode key={`code-${segIdx}`} language={seg.language || 'plaintext'} code={seg.code} />
          )
        }
        return (
          <ReactMarkdown key={`prose-${segIdx}`} remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
            {seg.text}
          </ReactMarkdown>
        )
      })}
    </div>
  )
}

// Inline code — fenced blocks are already extracted by splitFencedCodeBlocks,
// so a `language-` class or embedded newline here means an indented code block
// (rare); let the <pre> override style those. Inline `src/…:line` tokens become
// jump-to-code buttons, everything else a code chip.
function MdCode({ className, children }: React.HTMLAttributes<HTMLElement>) {
  const requestCodeNav = useChatStore((s) => s.requestCodeNav)
  const text = String(children ?? '')

  if (className?.includes('language-') || text.includes('\n')) {
    return <code className={className}>{children}</code>
  }

  const ref = parseCodeRef(text)
  if (ref) {
    return (
      <button
        type="button"
        onClick={() => requestCodeNav(ref)}
        className="rounded bg-primary/10 px-1.5 py-0.5 text-xs font-mono text-primary underline-offset-2 transition-colors hover:bg-primary/20 hover:underline"
        title={`Jump to ${ref.path}`}
      >
        {text}
      </button>
    )
  }
  return <code className="rounded bg-background/50 px-1.5 py-0.5 text-xs font-mono">{children}</code>
}

// Markdown element styling, scaled to the bubble's text-sm and kept compact.
// Colors inherit from the bubble (user bubbles are primary-foreground text),
// so use text-current/opacity rather than fixed foreground colors where the
// element must work in both bubble variants.
const MD_COMPONENTS: Components = {
  code: MdCode,
  p: ({ children }) => <p className="my-1 leading-relaxed first:mt-0 last:mb-0">{children}</p>,
  h1: ({ children }) => <h1 className="mb-1 mt-3 text-base font-semibold first:mt-0">{children}</h1>,
  h2: ({ children }) => <h2 className="mb-1 mt-3 text-base font-semibold first:mt-0">{children}</h2>,
  h3: ({ children }) => <h3 className="mb-1 mt-2 text-sm font-semibold first:mt-0">{children}</h3>,
  h4: ({ children }) => <h4 className="mb-1 mt-2 text-sm font-semibold first:mt-0">{children}</h4>,
  h5: ({ children }) => <h5 className="mb-1 mt-2 text-sm font-semibold first:mt-0">{children}</h5>,
  h6: ({ children }) => <h6 className="mb-1 mt-2 text-sm font-semibold first:mt-0">{children}</h6>,
  ul: ({ children }) => <ul className="my-1 list-disc space-y-0.5 pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="my-1 list-decimal space-y-0.5 pl-5">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="font-medium underline underline-offset-2 hover:opacity-80"
    >
      {children}
    </a>
  ),
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  blockquote: ({ children }) => (
    <blockquote className="my-1 border-l-2 border-current/30 pl-3 opacity-80">{children}</blockquote>
  ),
  pre: ({ children }) => (
    <pre className="my-1 overflow-x-auto rounded-lg bg-background/60 p-3 text-xs font-mono">{children}</pre>
  ),
  hr: () => <hr className="my-2 border-current/20" />,
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto">
      <table className="w-full border-collapse text-xs">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-border px-2 py-1 text-left font-semibold">{children}</th>
  ),
  td: ({ children }) => <td className="border border-border px-2 py-1 align-top">{children}</td>,
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
