/**
 * MermaidBlock — renders ```mermaid fences in chat as actual diagrams
 * (flowcharts, sequence diagrams, ER diagrams) so planning conversations get
 * pictures instead of syntax. Falls back to a quiet code view while the fence
 * is still streaming in (or if the syntax is invalid).
 */
import { useEffect, useRef, useState } from 'react'
import mermaid from 'mermaid'
import { CodeBlock } from './CodeBlock'

let initialized = false
let renderSeq = 0

export function MermaidBlock({ code }: { code: string }) {
  const [svg, setSvg] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  const idRef = useRef(`mmd-${++renderSeq}`)

  useEffect(() => {
    if (!initialized) {
      mermaid.initialize({
        startOnLoad: false,
        theme: 'dark',
        // strict: mermaid sanitizes labels; no scripts/clicks in output.
        securityLevel: 'strict',
      })
      initialized = true
    }
    let alive = true
    mermaid.render(idRef.current, code)
      .then(({ svg }) => {
        if (alive) {
          setSvg(svg)
          setFailed(false)
        }
      })
      .catch(() => {
        // Mid-stream partial fences land here constantly — stay quiet.
        if (alive) setFailed(true)
      })
    return () => {
      alive = false
    }
  }, [code])

  if (svg) {
    return (
      <div className="my-2 overflow-x-auto rounded-lg border border-border bg-background p-3 [&_svg]:mx-auto [&_svg]:max-w-full">
        <div dangerouslySetInnerHTML={{ __html: svg }} />
      </div>
    )
  }
  if (failed) {
    return <CodeBlock language="mermaid" code={code} />
  }
  return (
    <div className="my-2 rounded-lg border border-border bg-background p-3 text-xs text-muted-foreground">
      Rendering diagram…
    </div>
  )
}
