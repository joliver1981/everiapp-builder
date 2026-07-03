/**
 * Lightweight passive capture of context useful for bug reports.
 * Patches console + fetch on first import. Cheap; survives across renders.
 */

const CONSOLE_TAIL_MAX = 50
const NETWORK_ERRORS_MAX = 10

export interface CapturedNetworkError {
  url: string
  status: number | null
  method: string
  error: string | null
  timestamp: number
}

const consoleTail: string[] = []
const networkErrors: CapturedNetworkError[] = []

let installed = false

function pushConsole(level: string, args: unknown[]): void {
  try {
    const line = args
      .map((a) => {
        if (typeof a === 'string') return a
        if (a instanceof Error) return `${a.name}: ${a.message}`
        try {
          return JSON.stringify(a)
        } catch {
          return String(a)
        }
      })
      .join(' ')
    const ts = new Date().toISOString().slice(11, 23)
    consoleTail.push(`${ts} [${level}] ${line.slice(0, 500)}`)
    while (consoleTail.length > CONSOLE_TAIL_MAX) consoleTail.shift()
  } catch {
    /* ignore */
  }
}

function pushNetwork(err: CapturedNetworkError): void {
  networkErrors.push(err)
  while (networkErrors.length > NETWORK_ERRORS_MAX) networkErrors.shift()
}

export function installBugCapture(): void {
  if (installed) return
  installed = true
  if (typeof window === 'undefined') return

  // Console patches
  const levels: Array<keyof Console> = ['log', 'info', 'warn', 'error']
  for (const level of levels) {
    const original = (console as any)[level]?.bind(console)
    if (typeof original !== 'function') continue
    ;(console as any)[level] = (...args: unknown[]) => {
      pushConsole(level as string, args)
      original(...args)
    }
  }

  // Uncaught errors
  window.addEventListener('error', (ev) => {
    pushConsole('error', [`uncaught: ${ev.message} @ ${ev.filename}:${ev.lineno}:${ev.colno}`])
  })
  window.addEventListener('unhandledrejection', (ev: PromiseRejectionEvent) => {
    const reason = ev.reason
    pushConsole('error', [`unhandledrejection: ${reason?.message ?? String(reason)}`])
  })

  // fetch wrapping — only failures get recorded. The tracing module's own
  // span-flush POSTs are excluded: during a platform outage they fail every
  // 500ms and would evict the app's REAL failing calls from this small ring.
  const isTelemetryUrl = (url: string) => /\/api\/apps\/[^/]+\/spans$/.test(url)
  const originalFetch = window.fetch
  if (typeof originalFetch === 'function') {
    window.fetch = async (input: any, init?: RequestInit): Promise<Response> => {
      const url = typeof input === 'string' ? input : (input?.url ?? String(input))
      const method = (init?.method || (typeof input === 'object' ? input?.method : '') || 'GET').toUpperCase()
      const ts = Date.now()
      try {
        const resp = await originalFetch(input, init)
        if (!resp.ok && !isTelemetryUrl(url)) {
          pushNetwork({ url, method, status: resp.status, error: null, timestamp: ts })
        }
        return resp
      } catch (e: any) {
        if (!isTelemetryUrl(url)) {
          pushNetwork({ url, method, status: null, error: e?.message || String(e), timestamp: ts })
        }
        throw e
      }
    }
  }
}

export function snapshotCapturedContext(): {
  page_url: string
  user_agent: string
  viewport: { width: number; height: number }
  console_tail: string[]
  network_errors: CapturedNetworkError[]
} {
  if (typeof window === 'undefined') {
    return {
      page_url: '',
      user_agent: '',
      viewport: { width: 0, height: 0 },
      console_tail: [],
      network_errors: [],
    }
  }
  return {
    page_url: window.location.href,
    user_agent: navigator.userAgent,
    viewport: { width: window.innerWidth, height: window.innerHeight },
    console_tail: [...consoleTail],
    network_errors: [...networkErrors],
  }
}

/** Take an HTML2Canvas screenshot, lazily importing the dep so it doesn't bloat the build. */
export async function captureScreenshot(): Promise<string | null> {
  if (typeof document === 'undefined') return null
  try {
    // Use a CDN URL imported dynamically so we don't have to bundle ~50KB.
    // Apps that want a hard dep can: `npm i html2canvas` and we'll prefer that.
    // Use a runtime-resolved specifier so the TS/Vite bundler doesn't try to
    // resolve the import at build time. Apps can `npm i html2canvas` for a
    // proper bundled dep; otherwise we fall back to the CDN copy.
    const moduleName = 'html2canvas'
    let html2canvas: any
    try {
      html2canvas = (await import(/* @vite-ignore */ moduleName)).default
    } catch {
      html2canvas = await loadHtml2CanvasFromCDN()
      if (!html2canvas) return null
    }
    const canvas = await html2canvas(document.body, {
      useCORS: true,
      scale: Math.min(window.devicePixelRatio || 1, 2),
      logging: false,
    })
    return canvas.toDataURL('image/png')
  } catch (e) {
    console.warn('[AIHub] screenshot capture failed:', e)
    return null
  }
}

async function loadHtml2CanvasFromCDN(): Promise<any | null> {
  if ((window as any).html2canvas) return (window as any).html2canvas
  return new Promise((resolve) => {
    const script = document.createElement('script')
    script.src = 'https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js'
    script.onload = () => resolve((window as any).html2canvas || null)
    script.onerror = () => resolve(null)
    document.head.appendChild(script)
  })
}
