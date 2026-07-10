/**
 * callFunction — run one of this app's SERVER FUNCTIONS on the platform.
 *
 * A server function is a Python file the app itself defines at
 * server/functions/<name>.py with `def handler(args, ctx):` — it executes on
 * the platform host (not in the browser) with access to the app's database,
 * attached Connections, and AI calls through its `ctx`. Use one for data
 * crunching (pandas), document generation, or multi-step orchestration that
 * shouldn't live in the browser; anything a single SDK call already does
 * should stay client-side.
 *
 *   const summary = await callFunction<{ total: number }>('summarize-orders', {
 *     since: '2026-01-01',
 *   })
 *   // summary is exactly what handler() returned
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { hasSessionToken, sessionExpiredError } from './session'

declare global {
  interface Window {
    __AIHUB_APP_ID__?: string
    __AIHUB_TOKEN__?: string
  }
}

const AIHUB_BASE: string =
  ((import.meta as any).env?.VITE_AIHUB_BASE_URL as string | undefined) || ''

interface FunctionInvokeEnvelope<T> {
  ok: boolean
  result: T
  /** ctx.log()/print() output from the function, for debugging. */
  logs: string[]
  duration_ms: number
}

/** Invoke a server function with JSON args; resolves to the function's return value. */
export async function callFunction<T = unknown>(name: string, args?: unknown): Promise<T> {
  const appId = typeof window !== 'undefined' ? window.__AIHUB_APP_ID__ : undefined
  if (!appId) throw new Error('callFunction: window.__AIHUB_APP_ID__ is not set')
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (window.__AIHUB_TOKEN__) headers['Authorization'] = `Bearer ${window.__AIHUB_TOKEN__}`

  const resp = await fetch(`${AIHUB_BASE}/api/apps/${appId}/fn/${encodeURIComponent(name)}`, {
    method: 'POST',
    headers,
    credentials: 'include',
    body: JSON.stringify({ args: args ?? null }),
  })
  if (resp.status === 401 && hasSessionToken()) throw sessionExpiredError()
  if (!resp.ok) {
    let detail = ''
    try {
      detail = (await resp.json())?.detail ?? ''
    } catch {
      /* ignore */
    }
    throw new Error(`callFunction: HTTP ${resp.status}${detail ? ` — ${detail}` : ''}`)
  }
  const envelope = (await resp.json()) as FunctionInvokeEnvelope<T>
  return envelope.result
}

export interface UseFunctionState<T> {
  call: (args?: unknown) => Promise<T>
  lastResult: T | null
  isLoading: boolean
  error: Error | null
}

/** React hook wrapper around callFunction for a fixed function name. */
export function useFunction<T = unknown>(name: string): UseFunctionState<T> {
  const [lastResult, setLastResult] = useState<T | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  const call = useCallback(async (args?: unknown) => {
    setIsLoading(true)
    setError(null)
    try {
      const result = await callFunction<T>(name, args)
      setLastResult(result)
      return result
    } catch (e: any) {
      setError(e instanceof Error ? e : new Error(String(e)))
      throw e
    } finally {
      setIsLoading(false)
    }
  }, [name])

  return { call, lastResult, isLoading, error }
}

export interface AppFunction {
  /** The function's name — pass to callFunction. */
  name: string
  /** Execution runtime ('python'). */
  runtime: string
  /** Hard per-invocation timeout in seconds. */
  timeout_s: number
}

/** listFunctions — this app's server functions, discovered at runtime. */
export async function listFunctions(): Promise<AppFunction[]> {
  const appId = typeof window !== 'undefined' ? window.__AIHUB_APP_ID__ : undefined
  if (!appId) throw new Error('listFunctions: window.__AIHUB_APP_ID__ is not set')
  const headers: Record<string, string> = {}
  if (window.__AIHUB_TOKEN__) headers['Authorization'] = `Bearer ${window.__AIHUB_TOKEN__}`

  const resp = await fetch(`${AIHUB_BASE}/api/apps/${appId}/fn`, {
    headers,
    credentials: 'include',
  })
  if (resp.status === 401 && hasSessionToken()) throw sessionExpiredError()
  if (!resp.ok) {
    let detail = ''
    try {
      detail = (await resp.json())?.detail ?? ''
    } catch {
      /* ignore */
    }
    throw new Error(`listFunctions: HTTP ${resp.status}${detail ? ` — ${detail}` : ''}`)
  }
  return (await resp.json()) as AppFunction[]
}

export interface UseFunctionsState {
  /** This app's server functions; null while first-loading or after an error. */
  functions: AppFunction[] | null
  loading: boolean
  error: Error | null
  refetch: () => void
}

/** React hook wrapper around listFunctions — fetches once on mount. */
export function useFunctions(): UseFunctionsState {
  const [state, setState] = useState<{
    functions: AppFunction[] | null
    loading: boolean
    error: Error | null
  }>({ functions: null, loading: true, error: null })
  const mountedRef = useRef(true)

  const run = useCallback(async () => {
    setState((s) => ({ ...s, loading: true, error: null }))
    try {
      const functions = await listFunctions()
      if (!mountedRef.current) return
      setState({ functions, loading: false, error: null })
    } catch (e) {
      if (!mountedRef.current) return
      const err = e instanceof Error ? e : new Error(String(e))
      setState({ functions: null, loading: false, error: err })
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    run()
    return () => {
      mountedRef.current = false
    }
  }, [run])

  return { ...state, refetch: run }
}
