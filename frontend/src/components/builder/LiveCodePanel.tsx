import { Code2, Loader2 } from 'lucide-react'
import { useChatStore } from '@/stores/chatStore'
import { CodeEditor } from '@/components/editor/CodeEditor'

function langForPath(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase()
  switch (ext) {
    case 'tsx':
    case 'ts':
      return 'typescript'
    case 'jsx':
    case 'js':
      return 'javascript'
    case 'css':
      return 'css'
    case 'json':
      return 'json'
    case 'html':
      return 'html'
    case 'md':
      return 'markdown'
    default:
      return 'plaintext'
  }
}

// Read-only side panel that shows the AI writing each file live, fed by `code_stream`
// events accumulated in chatStore.liveCode. Opened via the builder's "Live" toggle.
export function LiveCodePanel() {
  const liveCode = useChatStore((s) => s.liveCode)
  const isStreaming = useChatStore((s) => s.isStreaming)

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h3 className="text-sm font-semibold">Live Code</h3>
        {liveCode && (
          <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
            {isStreaming ? (
              <>
                <Loader2 size={11} className="animate-spin text-primary" />
                writing
              </>
            ) : (
              'wrote'
            )}
            <span className="max-w-40 truncate font-mono text-foreground" title={liveCode.path}>
              {liveCode.path.split('/').pop()}
            </span>
          </span>
        )}
      </div>
      <div className="min-h-0 flex-1">
        {liveCode ? (
          <CodeEditor
            value={liveCode.content}
            language={langForPath(liveCode.path)}
            readOnly
            stickToBottom={isStreaming}
          />
        ) : (
          <div className="flex h-full flex-col items-center justify-center px-6 text-center">
            <Code2 size={36} className="text-muted-foreground/30" />
            <p className="mt-3 text-sm text-muted-foreground">Watch the AI write code live</p>
            <p className="mt-1 text-xs text-muted-foreground/70">
              Send a message and each file the AI writes will stream in here as it's generated.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
