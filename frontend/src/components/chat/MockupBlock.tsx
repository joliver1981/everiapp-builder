/**
 * MockupBlock — renders ```mockup (self-contained HTML) and ```svg fences in
 * chat as visual screen mockups, sandboxed (no scripts, no external network).
 * A mockup here IS proto-code, so the block carries "Make this real": one
 * click asks the builder to implement the approved design as actual screens.
 */
import { useState } from 'react'
import { Code2, Eye, Hammer, Loader2 } from 'lucide-react'
import { useChatStore } from '@/stores/chatStore'
import { useAppStore } from '@/stores/appStore'
import { CodeBlock } from './CodeBlock'
import { cn } from '@/lib/utils'

function srcDocFor(language: string, code: string): string {
  const body = language === 'svg' ? code : code
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    body { margin: 0; padding: 12px; font-family: system-ui, -apple-system, sans-serif; background: #fff; color: #111; }
    * { box-sizing: border-box; }
    img { max-width: 100%; }
  </style></head><body>${body}</body></html>`
}

export function MockupBlock({ language, code }: { language: string; code: string }) {
  const [view, setView] = useState<'preview' | 'source'>('preview')
  const sendMessage = useChatStore((s) => s.sendMessage)
  const isStreaming = useChatStore((s) => s.isStreaming)
  const appId = useAppStore((s) => s.currentApp?.id)

  const makeReal = () => {
    if (!appId || isStreaming) return
    sendMessage(
      appId,
      'I approve the mockup you showed above (the ```' + language + ' block). ' +
      'Implement it as a real screen in the app — same layout, structure, and ' +
      'labels, built with real components, wired to real data where the mockup ' +
      'implies it, following the app\'s existing conventions.',
    )
  }

  return (
    <div className="my-2 overflow-hidden rounded-lg border border-border">
      <div className="flex items-center justify-between bg-muted px-3 py-1.5">
        <span className="text-xs text-muted-foreground">mockup</span>
        <div className="flex items-center gap-1">
          <button
            onClick={() => setView(view === 'preview' ? 'source' : 'preview')}
            className="flex items-center gap-1 rounded px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
            title={view === 'preview' ? 'View the mockup markup' : 'Back to the rendered mockup'}
          >
            {view === 'preview' ? <Code2 size={12} /> : <Eye size={12} />}
            {view === 'preview' ? 'Source' : 'Preview'}
          </button>
          <button
            onClick={makeReal}
            disabled={!appId || isStreaming}
            className={cn(
              'flex items-center gap-1 rounded-md bg-primary px-2.5 py-1 text-[11px] font-medium',
              'text-primary-foreground hover:bg-primary/90 disabled:opacity-50',
            )}
            title={isStreaming
              ? 'A build is already running — wait for it to finish'
              : 'Ask the AI to implement this mockup as a real screen in the app (same layout and labels, real components and data)'}
          >
            {isStreaming ? <Loader2 size={11} className="animate-spin" /> : <Hammer size={11} />}
            Make this real
          </button>
        </div>
      </div>
      {view === 'preview' ? (
        <iframe
          sandbox=""
          srcDoc={srcDocFor(language, code)}
          title="Screen mockup"
          className="h-96 w-full border-0 bg-white"
        />
      ) : (
        <CodeBlock language={language} code={code} />
      )}
    </div>
  )
}
