/**
 * useDatasetMutation — write back to the customer's CENTRAL database via a
 * dataset that has a mutation_sql defined and a writable (non-read-only)
 * connection.
 *
 * Mirrors useDataset (read) but POSTs to the /mutate endpoint. The platform
 * enforces: app must be bound to the dataset, connection must not be read-only,
 * mutation_sql must be present. Returns rows_affected.
 *
 * Usage:
 *   const { mutate, loading, error } = useDatasetMutation('update_product_price')
 *   await mutate({ product_id: id, new_price: price })
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

export interface DatasetMutationResult {
  rows_affected: number
  duration_ms: number
}

export function useDatasetMutation(datasetId: string) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  const mutate = useCallback(
    async (params: Record<string, unknown> = {}): Promise<DatasetMutationResult> => {
      const appId = window.__AIHUB_APP_ID__
      if (!appId) {
        throw new Error('useDatasetMutation: window.__AIHUB_APP_ID__ is not set')
      }
      setLoading(true)
      setError(null)
      try {
        const headers: Record<string, string> = { 'Content-Type': 'application/json' }
        const token = window.__AIHUB_TOKEN__
        if (token) headers['Authorization'] = `Bearer ${token}`

        const resp = await fetch(
          `${AIHUB_BASE}/api/apps/${appId}/datasets/${datasetId}/mutate`,
          {
            method: 'POST',
            headers,
            credentials: 'include',
            body: JSON.stringify({ params }),
          },
        )
        if (!resp.ok) {
          const text = await resp.text().catch(() => '')
          throw new Error(`Mutation failed (${resp.status}): ${text || resp.statusText}`)
        }
        return (await resp.json()) as DatasetMutationResult
      } catch (e) {
        const err = e instanceof Error ? e : new Error(String(e))
        setError(err)
        throw err
      } finally {
        setLoading(false)
      }
    },
    [datasetId],
  )

  return { mutate, loading, error }
}
