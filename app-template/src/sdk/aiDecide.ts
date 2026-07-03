/**
 * aiDecide — mini-LLM decisions as a first-class primitive.
 *
 * For fuzzy logic (classification, extraction, intent routing, ranking,
 * normalization) apps call a NAMED decision instead of writing brittle
 * regex/keyword heuristics:
 *
 *   const route = await aiDecide<'follow_up' | 'new_query'>(
 *     'classify_question', { question, history })
 *   // route.value: 'follow_up' | 'new_query'
 *
 * The call site carries only the name and an input object. The prompt, output
 * schema, model, and fallback live server-side in the decision registry
 * (declared via decisions.json) — admins can tune the prompt in the platform
 * and the very next invocation uses it, with zero rebuild.
 *
 * Failure semantics: the SERVER resolves LLM trouble (timeout, provider
 * error, bad output) to the decision's declared fallback. If the platform
 * itself is unreachable, aiDecide resolves to `opts.fallback` when given,
 * else rejects — so pass a fallback in apps that must work offline.
 */

import { useCallback, useState } from 'react'

declare global {
  interface Window {
    __AIHUB_APP_ID__?: string
    __AIHUB_TOKEN__?: string
  }
}

const AIHUB_BASE: string =
  ((import.meta as any).env?.VITE_AIHUB_BASE_URL as string | undefined) || ''

export interface DecisionResult<T = unknown> {
  value: T
  source: 'llm' | 'cache' | 'fallback'
  latency_ms: number
}

export interface AiDecideOptions<T = unknown> {
  /** Resolve to this (source 'fallback') when the PLATFORM is unreachable. */
  fallback?: T
}

export async function aiDecide<T = unknown>(
  name: string,
  input: Record<string, unknown> = {},
  opts: AiDecideOptions<T> = {},
): Promise<DecisionResult<T>> {
  const appId = typeof window !== 'undefined' ? window.__AIHUB_APP_ID__ : undefined
  if (!appId) throw new Error('aiDecide: window.__AIHUB_APP_ID__ is not set')
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (window.__AIHUB_TOKEN__) headers['Authorization'] = `Bearer ${window.__AIHUB_TOKEN__}`

  let resp: Response
  try {
    resp = await fetch(`${AIHUB_BASE}/api/decisions/${appId}/${name}/invoke`, {
      method: 'POST',
      headers,
      credentials: 'include',
      body: JSON.stringify({ input }),
    })
  } catch (err) {
    // Platform unreachable — the ONLY case opts.fallback covers. HTTP errors
    // below (404 undeclared decision, 401 auth, 5xx) always throw: silently
    // falling back there would mask real bugs like name drift.
    if ('fallback' in opts) {
      return { value: opts.fallback as T, source: 'fallback', latency_ms: 0 }
    }
    throw err
  }
  if (!resp.ok) throw new Error(`aiDecide '${name}': HTTP ${resp.status}`)
  return (await resp.json()) as DecisionResult<T>
}

export interface UseDecisionState<T> {
  decide: (input?: Record<string, unknown>) => Promise<DecisionResult<T>>
  lastResult: DecisionResult<T> | null
  isLoading: boolean
  error: Error | null
}

/** React hook wrapper: `const { decide, lastResult, isLoading } = useDecision('classify_question')` */
export function useDecision<T = unknown>(
  name: string,
  opts: AiDecideOptions<T> = {},
): UseDecisionState<T> {
  const [lastResult, setLastResult] = useState<DecisionResult<T> | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  const decide = useCallback(async (input: Record<string, unknown> = {}) => {
    setIsLoading(true)
    setError(null)
    try {
      const result = await aiDecide<T>(name, input, opts)
      setLastResult(result)
      return result
    } catch (e: any) {
      setError(e instanceof Error ? e : new Error(String(e)))
      throw e
    } finally {
      setIsLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, JSON.stringify(opts.fallback)])

  return { decide, lastResult, isLoading, error }
}
