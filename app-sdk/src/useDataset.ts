/**
 * useDataset — Fetch rows from a platform-managed dataset.
 *
 * The platform injects `window.__AIHUB_APP_ID__` and `window.__AIHUB_TOKEN__`
 * via the runtime proxy. This hook POSTs to
 *   /api/apps/{app_id}/datasets/{dataset_id}/execute
 * with the given params, returning rows + a small status envelope.
 *
 * Usage:
 *   const { data, loading, error, refetch } = useDataset<Order>('recent_orders', {
 *     customer_id: customerId,
 *   })
 *
 * The dataset id is whatever the platform's Datasets page gave you. Params is
 * a JSON object matching the dataset's declared parameter schema. The platform
 * automatically injects `current_user`, so the dataset can filter by the
 * calling user without the app having to pass it.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { platformError } from './session'

declare global {
  interface Window {
    __AIHUB_APP_ID__?: string
    __AIHUB_TOKEN__?: string
  }
}

const AIHUB_BASE: string =
  ((import.meta as any).env?.VITE_AIHUB_BASE_URL as string | undefined) || ''

export interface DatasetColumn {
  name: string
  type: string
}

export interface DatasetResult<TRow = Record<string, unknown>> {
  rows: TRow[]
  columns: DatasetColumn[]
  row_count: number
  truncated: boolean
  duration_ms: number
}

export interface UseDatasetState<TRow> {
  data: TRow[] | null
  result: DatasetResult<TRow> | null
  loading: boolean
  error: Error | null
  refetch: () => void
}

interface UseDatasetOptions {
  /** Skip the auto-fetch on mount; only run when refetch() is called. */
  skip?: boolean
}

function getAppId(): string | null {
  return window.__AIHUB_APP_ID__ || null
}

function getToken(): string | null {
  return window.__AIHUB_TOKEN__ || null
}

export async function executeDataset<TRow = Record<string, unknown>>(
  datasetId: string,
  params: Record<string, unknown> = {},
): Promise<DatasetResult<TRow>> {
  const appId = getAppId()
  if (!appId) {
    throw new Error(
      'useDataset: window.__AIHUB_APP_ID__ is not set. Are you running inside an AIHub-deployed app?',
    )
  }
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`

  const resp = await fetch(
    `${AIHUB_BASE}/api/apps/${appId}/datasets/${datasetId}/execute`,
    {
      method: 'POST',
      headers,
      credentials: 'include',
      body: JSON.stringify({ params }),
    },
  )
  if (!resp.ok) {
    const text = await resp.text().catch(() => '')
    // 401 → the injected token expired: friendly message + 'aihub:token-expired'
    // event instead of a raw {"detail":"Invalid or expired token"} in the UI.
    throw platformError('Dataset execute', resp.status, text || resp.statusText)
  }
  return (await resp.json()) as DatasetResult<TRow>
}

/**
 * React hook wrapper around executeDataset.
 *
 * Re-fetches when datasetId or stringified params change. Use the returned
 * refetch() to manually trigger a re-run with the same params.
 */
export function useDataset<TRow = Record<string, unknown>>(
  datasetId: string,
  params: Record<string, unknown> = {},
  options: UseDatasetOptions = {},
): UseDatasetState<TRow> {
  const [state, setState] = useState<{
    data: TRow[] | null
    result: DatasetResult<TRow> | null
    loading: boolean
    error: Error | null
  }>({ data: null, result: null, loading: !options.skip, error: null })

  // Stringify params for dep-array comparison without re-rendering on identical
  // object identities.
  const paramsKey = JSON.stringify(params)
  const mountedRef = useRef(true)

  const run = useCallback(async () => {
    setState((s) => ({ ...s, loading: true, error: null }))
    try {
      const result = await executeDataset<TRow>(datasetId, params)
      if (!mountedRef.current) return
      setState({ data: result.rows, result, loading: false, error: null })
    } catch (e) {
      if (!mountedRef.current) return
      const err = e instanceof Error ? e : new Error(String(e))
      setState({ data: null, result: null, loading: false, error: err })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasetId, paramsKey])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  useEffect(() => {
    if (options.skip) return
    run()
  }, [run, options.skip])

  return {
    data: state.data,
    result: state.result,
    loading: state.loading,
    error: state.error,
    refetch: run,
  }
}
