/**
 * useAppQuery / useAppMutation / useAppSchema — the per-app SQLite store.
 *
 * Every deployed app gets its OWN SQLite database for local state (todos,
 * notes, drafts, settings — anything the app itself creates). These hooks
 * talk to the platform's /api/apps/{app_id}/db/* endpoints. The platform
 * auto-injects `current_user` into every query, so the app can scope data
 * per-user without passing identity around.
 *
 * When to use this vs useDataset:
 *   - useAppDB*   → the app's OWN data (this app created it)
 *   - useDataset  → the customer's CENTRAL data (sales, inventory, ERP)
 *
 * Usage:
 *   // Declare schema once near the top of your app
 *   useAppSchema(`
 *     CREATE TABLE IF NOT EXISTS todos (
 *       id INTEGER PRIMARY KEY AUTOINCREMENT,
 *       title TEXT NOT NULL, done BOOLEAN DEFAULT 0,
 *       created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
 *     )
 *   `)
 *
 *   const { data, loading, error, refetch } = useAppQuery<Todo>(
 *     'SELECT * FROM todos ORDER BY created_at DESC'
 *   )
 *
 *   const { mutate } = useAppMutation('INSERT INTO todos (title) VALUES (:title)')
 *   await mutate({ title: 'Ship it' })
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

function getAppId(): string | null {
  return window.__AIHUB_APP_ID__ || null
}

function authHeaders(): Record<string, string> {
  const h: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = window.__AIHUB_TOKEN__
  if (token) h['Authorization'] = `Bearer ${token}`
  return h
}

async function postDb<T>(path: string, payload: unknown): Promise<T> {
  const appId = getAppId()
  if (!appId) {
    throw new Error(
      'useAppDB: window.__AIHUB_APP_ID__ is not set. Are you running inside an AIHub app?',
    )
  }
  const resp = await fetch(`${AIHUB_BASE}/api/apps/${appId}/db/${path}`, {
    method: 'POST',
    headers: authHeaders(),
    credentials: 'include',
    body: JSON.stringify(payload),
  })
  if (!resp.ok) {
    const text = await resp.text().catch(() => '')
    // 401 → the injected token expired: friendly message + 'aihub:token-expired'
    // event instead of a raw {"detail":"Invalid or expired token"} in the UI.
    throw platformError(`app-db ${path}`, resp.status, text || resp.statusText)
  }
  return (await resp.json()) as T
}

// ---------------------------------------------------------------------------
// Schema-readiness gate
//
// useAppSchema runs CREATE TABLE as an async migration on mount; useAppQuery
// runs its SELECT on mount too. Without coordination the SELECT can reach the
// backend before the table exists → "no such table" 400 on first load. This
// module-level gate makes every useAppQuery wait for ALL declared schema
// migrations to finish first (an app may declare schemas from several
// hooks/components) — but never hangs apps that declare no schema.
// ---------------------------------------------------------------------------
let _schemaRegistered = false
let _schemaPendingCount = 0
let _schemaSettle: Promise<void> | null = null
let _resolveSchema: (() => void) | null = null

function _ensureSchemaPromise(): void {
  if (!_schemaSettle) {
    _schemaSettle = new Promise<void>((resolve) => {
      _resolveSchema = resolve
    })
  }
}

function _markSchemaRegistered(): void {
  _schemaRegistered = true
  _schemaPendingCount++
  _ensureSchemaPromise()
}

function _markSchemaSettled(): void {
  _ensureSchemaPromise()
  _schemaPendingCount--
  if (_schemaPendingCount <= 0) _resolveSchema?.()
}

async function _waitForSchema(): Promise<void> {
  // Let a sibling/parent useAppSchema effect register first. Effects flush in
  // the same task; yielding a macrotask guarantees registration has happened.
  if (!_schemaRegistered) {
    await new Promise<void>((r) => setTimeout(() => r(), 0))
  }
  if (_schemaRegistered && _schemaSettle) {
    // Wait for the migration to finish, but never hang forever.
    await Promise.race([_schemaSettle, new Promise<void>((r) => setTimeout(() => r(), 8000))])
  }
}

/** Full response envelope for a query. `truncated` is true when the row cap was
 *  hit — raise `limit` (or page) to get the rest. `result.rows === data`. */
export interface AppQueryEnvelope<TRow> {
  rows: TRow[]
  columns: string[]
  row_count: number
  truncated: boolean
}

export interface AppQueryResult<TRow> {
  data: TRow[] | null
  /** The full response envelope (columns, row_count, truncated). Null until the
   *  first successful run. Check `result.truncated` to detect a capped result. */
  result: AppQueryEnvelope<TRow> | null
  loading: boolean
  error: Error | null
  refetch: () => void
}

interface AppQueryOptions {
  /** 'user' adds WHERE created_by = :current_user; 'all' (default) is shared. */
  scope?: 'all' | 'user'
  /** Skip the auto-run on mount; call refetch() to run. */
  skip?: boolean
  /** Max rows to return. Omitted → a generous server default. Raise for large
   *  result sets; the server clamps to an absolute ceiling. */
  limit?: number
}

/** Run a SELECT against the app's own SQLite store. */
export function useAppQuery<TRow = Record<string, unknown>>(
  sql: string,
  params: Record<string, unknown> = {},
  options: AppQueryOptions = {},
): AppQueryResult<TRow> {
  const [data, setData] = useState<TRow[] | null>(null)
  const [result, setResult] = useState<AppQueryEnvelope<TRow> | null>(null)
  const [loading, setLoading] = useState(!options.skip)
  const [error, setError] = useState<Error | null>(null)
  const paramsKey = JSON.stringify(params)
  const mounted = useRef(true)

  const run = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      await _waitForSchema()
      const res = await postDb<AppQueryEnvelope<TRow>>('query', {
        sql,
        params,
        scope: options.scope ?? 'all',
        ...(options.limit != null ? { limit: options.limit } : {}),
      })
      if (mounted.current) {
        setData(res.rows)
        setResult(res)
      }
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e : new Error(String(e)))
    } finally {
      if (mounted.current) setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sql, paramsKey, options.scope, options.limit])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    if (!options.skip) run()
  }, [run, options.skip])

  return { data, result, loading, error, refetch: run }
}

export interface AppMutationResult {
  rows_affected: number
  last_insert_rowid: number | null
}

/** Get a mutate() that runs an INSERT/UPDATE/DELETE against the app's store. */
export function useAppMutation(sql: string) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  const mutate = useCallback(
    async (params: Record<string, unknown> = {}): Promise<AppMutationResult> => {
      setLoading(true)
      setError(null)
      try {
        return await postDb<AppMutationResult>('exec', { sql, params })
      } catch (e) {
        const err = e instanceof Error ? e : new Error(String(e))
        setError(err)
        throw err
      } finally {
        setLoading(false)
      }
    },
    [sql],
  )

  return { mutate, loading, error }
}

// Tiny stable content hash (FNV-1a, hex) — gives each schema declaration its
// own migration name so independent useAppSchema calls don't collide.
function _fnv1a(s: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0).toString(16).padStart(8, '0')
}

interface MigrateResponse {
  applied_versions: number[]
  refused?: { version: number; name: string; reason: string }[]
  current_version?: number
  error?: string
}

/**
 * Declare (part of) the app's schema. On first mount the SDK applies it as a
 * migration; the migration's identity comes from the SQL's content, so an app
 * may declare schemas from SEVERAL hooks/components — each declaration is
 * applied independently, exactly once. For evolving schemas, prefer numbered
 * migrations applied by an admin via the platform — but useAppSchema is the
 * quick path for simple apps. Idempotent: CREATE TABLE IF NOT EXISTS means
 * re-runs are no-ops.
 */
export function useAppSchema(schemaSql: string) {
  const [ready, setReady] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const ran = useRef(false)

  useEffect(() => {
    if (ran.current) return
    ran.current = true
    _markSchemaRegistered()
    ;(async () => {
      try {
        const res = await postDb<MigrateResponse>('migrate', {
          migrations: [
            { version: 1, name: `app_schema_${_fnv1a(schemaSql)}`, sql: schemaSql },
          ],
        })
        // A refused/errored migration comes back HTTP 200 — surface it loudly
        // instead of letting the app run against tables that don't exist.
        if (res.error || (res.refused && res.refused.length > 0)) {
          const reason = res.error ?? res.refused!.map((r) => r.reason).join('; ')
          throw new Error(`useAppSchema: schema was not applied — ${reason}`)
        }
        setReady(true)
      } catch (e) {
        const err = e instanceof Error ? e : new Error(String(e))
        console.error('[AIHub SDK] useAppSchema failed:', err.message)
        setError(err)
      } finally {
        _markSchemaSettled()
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return { ready, error }
}
