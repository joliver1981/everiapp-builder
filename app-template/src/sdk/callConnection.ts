/**
 * callConnection — make a real outbound HTTP call from your app THROUGH an
 * admin-configured Connection.
 *
 * The Connection (set up in Admin → Connections and marked "app-callable", then
 * attached to this app) holds the base URL and the credential. Your app supplies
 * only the method, a RELATIVE path, and optionally query/headers/body — the
 * platform injects the base URL + auth server-side, so no API key ever lives in
 * the app bundle. This is how an app reaches an external API or a specific LLM
 * provider (one Connection per provider → a real side-by-side comparison).
 *
 *   const res = await callConnection('anthropic-conn', {
 *     method: 'POST',
 *     path: '/v1/messages',
 *     body: { model: 'claude-sonnet-5', max_tokens: 1024, messages: [...] },
 *   })
 *   // res.status (number), res.headers (object), res.body (parsed JSON or text)
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

export interface ConnectionCallRequest {
  /** HTTP method. Default 'GET'. */
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE' | 'HEAD'
  /** Path RELATIVE to the connection's base URL, e.g. '/v1/messages'. */
  path: string
  /** Query string params. */
  query?: Record<string, unknown>
  /** Extra request headers (the auth header is injected server-side and can't be overridden). */
  headers?: Record<string, unknown>
  /** Request body: an object/array is sent as JSON; a string is sent as-is. */
  body?: unknown
}

export interface ConnectionCallResult<T = unknown> {
  /** The upstream HTTP status code. */
  status: number
  /** Upstream response headers. */
  headers: Record<string, string>
  /** Parsed JSON when the response is JSON, otherwise the raw text. */
  body: T
  /** True if the response was larger than the platform's cap and was cut. */
  truncated: boolean
}

/** Make one outbound call through a bound, app-callable Connection. */
export async function callConnection<T = unknown>(
  connectionId: string,
  request: ConnectionCallRequest,
): Promise<ConnectionCallResult<T>> {
  const appId = typeof window !== 'undefined' ? window.__AIHUB_APP_ID__ : undefined
  if (!appId) throw new Error('callConnection: window.__AIHUB_APP_ID__ is not set')
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (window.__AIHUB_TOKEN__) headers['Authorization'] = `Bearer ${window.__AIHUB_TOKEN__}`

  const resp = await fetch(
    `${AIHUB_BASE}/api/apps/${appId}/connections/${connectionId}/call`,
    {
      method: 'POST',
      headers,
      credentials: 'include',
      body: JSON.stringify({
        method: request.method ?? 'GET',
        path: request.path,
        query: request.query,
        headers: request.headers,
        body: request.body,
      }),
    },
  )
  if (resp.status === 401 && hasSessionToken()) throw sessionExpiredError()
  if (!resp.ok) {
    let detail = ''
    try {
      detail = (await resp.json())?.detail ?? ''
    } catch {
      /* ignore */
    }
    throw new Error(`callConnection: HTTP ${resp.status}${detail ? ` — ${detail}` : ''}`)
  }
  return (await resp.json()) as ConnectionCallResult<T>
}

export interface UseConnectionCallState<T> {
  call: (request: ConnectionCallRequest) => Promise<ConnectionCallResult<T>>
  lastResult: ConnectionCallResult<T> | null
  isLoading: boolean
  error: Error | null
}

/** React hook wrapper around callConnection for a fixed connection id. */
export function useConnectionCall<T = unknown>(connectionId: string): UseConnectionCallState<T> {
  const [lastResult, setLastResult] = useState<ConnectionCallResult<T> | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  const call = useCallback(async (request: ConnectionCallRequest) => {
    setIsLoading(true)
    setError(null)
    try {
      const result = await callConnection<T>(connectionId, request)
      setLastResult(result)
      return result
    } catch (e: any) {
      setError(e instanceof Error ? e : new Error(String(e)))
      throw e
    } finally {
      setIsLoading(false)
    }
  }, [connectionId])

  return { call, lastResult, isLoading, error }
}

export interface AppConnection {
  /** Canonical connection id — pass this (or the name) to callConnection/aiChat. */
  id: string
  /** Human-readable connection name (also accepted by callConnection/aiChat). */
  name: string
  /** Admin-written description of what this connection reaches. */
  description: string
  /** The upstream base URL calls go to (informational — paths stay relative). */
  base_url: string
  /** 'rest' for generic HTTP APIs, 'ai' for AI-provider connections. */
  kind: string
  /** AI connections: 'openai' | 'anthropic' | 'openrouter' | 'azure_openai' | 'custom'. */
  provider: string | null
  /** AI connections: request-body dialect ('openai' | 'anthropic') — aiChat uses this. */
  api_format: 'openai' | 'anthropic' | null
  /** AI connections: the model ids the admin exposed for this connection. */
  models: string[]
  /** AI connections: the model to use when the app/user doesn't pick one. */
  default_model: string | null
  /** AI connections: chat endpoint path relative to base_url (e.g. '/messages'). */
  chat_path: string | null
}

/**
 * listConnections — the app-callable Connections currently ATTACHED to this app,
 * discovered at runtime. Attach/detach a connection in the builder and the next
 * call reflects it — no code change, no regeneration.
 *
 * Use this to drive any UI built around a variable set of connections (e.g. one
 * card per LLM provider): render whatever comes back, and when the list is empty
 * show a friendly "no connections attached yet" state instead of hardcoding ids.
 */
export async function listConnections(): Promise<AppConnection[]> {
  const appId = typeof window !== 'undefined' ? window.__AIHUB_APP_ID__ : undefined
  if (!appId) throw new Error('listConnections: window.__AIHUB_APP_ID__ is not set')
  const headers: Record<string, string> = {}
  if (window.__AIHUB_TOKEN__) headers['Authorization'] = `Bearer ${window.__AIHUB_TOKEN__}`

  const resp = await fetch(`${AIHUB_BASE}/api/apps/${appId}/connections`, {
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
    throw new Error(`listConnections: HTTP ${resp.status}${detail ? ` — ${detail}` : ''}`)
  }
  const items = (await resp.json()) as (Partial<AppConnection> & {
    id: string
    name: string
    app_callable?: boolean
  })[]
  // A binding can outlive its connection's app-callable flag; the app only
  // cares about what callConnection can actually reach.
  return items
    .filter((c) => c.app_callable !== false)
    .map((c) => ({
      id: c.id,
      name: c.name,
      description: c.description ?? '',
      base_url: c.base_url ?? '',
      kind: c.kind ?? 'rest',
      provider: c.provider ?? null,
      api_format: c.api_format ?? null,
      models: Array.isArray(c.models) ? c.models : [],
      default_model: c.default_model ?? null,
      chat_path: c.chat_path ?? null,
    }))
}

export interface UseConnectionsState {
  /** Attached app-callable connections; null while first-loading or after an error. */
  connections: AppConnection[] | null
  loading: boolean
  error: Error | null
  refetch: () => void
}

/** React hook wrapper around listConnections — fetches once on mount. */
export function useConnections(): UseConnectionsState {
  const [state, setState] = useState<{
    connections: AppConnection[] | null
    loading: boolean
    error: Error | null
  }>({ connections: null, loading: true, error: null })
  const mountedRef = useRef(true)

  const run = useCallback(async () => {
    setState((s) => ({ ...s, loading: true, error: null }))
    try {
      const connections = await listConnections()
      if (!mountedRef.current) return
      setState({ connections, loading: false, error: null })
    } catch (e) {
      if (!mountedRef.current) return
      const err = e instanceof Error ? e : new Error(String(e))
      setState({ connections: null, loading: false, error: err })
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
