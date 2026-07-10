import { useEffect, useRef } from 'react'
import Editor, { type OnMount } from '@monaco-editor/react'

type EditorInstance = Parameters<OnMount>[0]
type MonacoInstance = Parameters<OnMount>[1]

// Selection + viewport the editor reports up to the in-code overlay. The page adds the file
// path before sending it to the AI as EditorContext.
export interface EditorSelectionContext {
  selectionText: string | null
  selStartLine: number | null
  selEndLine: number | null
  viewportStartLine: number | null
  viewportEndLine: number | null
}

interface CodeEditorProps {
  value: string
  language: string
  onChange?: (value: string) => void
  readOnly?: boolean
  // Highlight + scroll to a line range. Bump `revealToken` to (re)trigger the reveal even
  // when the range is unchanged (so re-jumping to the same lines still scrolls there).
  highlight?: { startLine: number; endLine: number } | null
  revealToken?: number
  // Live view: keep the viewport pinned to the last line as content streams in.
  stickToBottom?: boolean
  // Report the user's current selection + on-screen range (for the collaboration overlay).
  onContextChange?: (ctx: EditorSelectionContext) => void
}

export function CodeEditor({
  value,
  language,
  onChange,
  readOnly = false,
  highlight = null,
  revealToken,
  stickToBottom = false,
  onContextChange,
}: CodeEditorProps) {
  const editorRef = useRef<EditorInstance | null>(null)
  const monacoRef = useRef<MonacoInstance | null>(null)
  const decorationsRef = useRef<ReturnType<EditorInstance['createDecorationsCollection']> | null>(null)
  // The last revealToken we actually acted on — guards against re-revealing on plain edits.
  const revealedRef = useRef<number | undefined>(undefined)
  // Kept fresh each render so the (once-registered) Monaco listeners always call the latest cb.
  const onContextChangeRef = useRef(onContextChange)
  onContextChangeRef.current = onContextChange
  const disposablesRef = useRef<{ dispose(): void }[]>([])
  const ctxRafRef = useRef<number | null>(null)

  // Reveal + highlight the target range. Safe to call repeatedly; it no-ops until the
  // editor is mounted, the target content has loaded, and the token is new. We depend on
  // `value` too so that when a freshly-opened file's content arrives a render later, we
  // retry and land the highlight on the right lines.
  function tryReveal() {
    const editor = editorRef.current
    const monaco = monacoRef.current
    if (!editor || !monaco || !highlight) return
    if (revealToken === undefined || revealToken === revealedRef.current) return
    const model = editor.getModel()
    if (!model || model.getLineCount() < highlight.startLine) return // content not ready yet
    revealedRef.current = revealToken
    editor.revealLineInCenter(highlight.startLine)
    decorationsRef.current?.set([
      {
        range: new monaco.Range(highlight.startLine, 1, highlight.endLine, 1),
        options: {
          isWholeLine: true,
          className: 'code-jump-highlight',
          linesDecorationsClassName: 'code-jump-highlight-glyph',
        },
      },
    ])
  }

  // Read the live selection + visible range and hand it to onContextChange.
  function reportContext() {
    const editor = editorRef.current
    const cb = onContextChangeRef.current
    if (!editor || !cb) return
    const model = editor.getModel()
    if (!model) return
    const sel = editor.getSelection()
    let selectionText: string | null = null
    let selStartLine: number | null = null
    let selEndLine: number | null = null
    if (sel && !sel.isEmpty()) {
      const text = model.getValueInRange(sel)
      selectionText = text.length > 32000 ? text.slice(0, 32000) : text
      selStartLine = sel.startLineNumber
      selEndLine = sel.endLineNumber
    }
    const visible = editor.getVisibleRanges()
    let viewportStartLine: number | null = null
    let viewportEndLine: number | null = null
    if (visible && visible.length > 0) {
      viewportStartLine = visible[0].startLineNumber
      viewportEndLine = visible[visible.length - 1].endLineNumber
    }
    cb({ selectionText, selStartLine, selEndLine, viewportStartLine, viewportEndLine })
  }

  // Coalesce the high-frequency selection/scroll events into one report per frame.
  function scheduleReport() {
    if (ctxRafRef.current != null) return
    ctxRafRef.current = requestAnimationFrame(() => {
      ctxRafRef.current = null
      reportContext()
    })
  }

  const handleMount: OnMount = (editor, monaco) => {
    editorRef.current = editor
    monacoRef.current = monaco
    decorationsRef.current = editor.createDecorationsCollection([])
    disposablesRef.current = [
      editor.onDidChangeCursorSelection(scheduleReport),
      editor.onDidScrollChange(scheduleReport),
    ]
    tryReveal()
    reportContext()
  }

  // Dispose Monaco listeners + any pending frame on unmount.
  useEffect(() => {
    return () => {
      disposablesRef.current.forEach((d) => d.dispose())
      disposablesRef.current = []
      if (ctxRafRef.current != null) cancelAnimationFrame(ctxRafRef.current)
    }
  }, [])

  useEffect(() => {
    tryReveal()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revealToken, value])

  // Clear the highlight when there's no target (e.g. the user switched to a different
  // file). @monaco-editor/react reuses one model via setValue, so a stale decoration
  // would otherwise bleed onto the next file at the same line numbers.
  useEffect(() => {
    if (!highlight) {
      decorationsRef.current?.clear()
      revealedRef.current = undefined
    }
  }, [highlight])

  useEffect(() => {
    if (!stickToBottom) return
    const editor = editorRef.current
    const model = editor?.getModel()
    if (editor && model) editor.revealLine(model.getLineCount())
  }, [value, stickToBottom])

  return (
    <Editor
      height="100%"
      language={language}
      value={value}
      onChange={(val) => onChange?.(val ?? '')}
      onMount={handleMount}
      theme="vs-dark"
      options={{
        readOnly,
        minimap: { enabled: false },
        fontSize: 13,
        fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
        lineHeight: 20,
        padding: { top: 16 },
        scrollBeyondLastLine: false,
        wordWrap: 'on',
        tabSize: 2,
        renderLineHighlight: 'line',
        cursorBlinking: 'smooth',
        smoothScrolling: true,
        bracketPairColorization: { enabled: true },
      }}
    />
  )
}
