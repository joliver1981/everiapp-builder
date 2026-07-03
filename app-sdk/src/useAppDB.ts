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
    throw new Error(`app-db ${path} failed (${resp.status}): ${text || resp.statusText}`)
  }
  return (await resp.json()) as T
}

// ---------------------------------------------------------------------------
// Schema-readiness gate
//
// useAppSchema runs CREATE TABLE as an async migration on mount; useAppQuery
// runs its SELECT on mount too. Without coordination the SELECT can reach the
// backend before the table exists → "no such table" 400 on first load. This
// module-level gate makes every useAppQuery wait for the (single) schema
// migration to finish first — but never hangs apps that declare no schema.
// ---------------------------------------------------------------------------
let _schemaRegistered = false
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
  _ensureSchemaPromise()
}

function _markSchemaSettled(): void {
  _ensureSchemaPromise()
  _resolveSchema?.()
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

export interface AppQueryResult<TRow> {
  data: TRow[] | null
  loading: boolean
  error: Error | null
  refetch: () => void
}

interface AppQueryOptions {
  /** 'user' adds WHERE created_by = :current_user; 'all' (default) is shared. */
  scope?: 'all' | 'user'
  /** Skip the auto-run on mount; call refetch() to run. */
  skip?: boolean
}

/** Run a SELECT against the app's own SQLite store. */
export function useAppQuery<TRow = Record<string, unknown>>(
  sql: string,
  params: Record<string, unknown> = {},
  options: AppQueryOptions = {},
): AppQueryResult<TRow> {
  const [data, setData] = useState<TRow[] | null>(null)
  const [loading, setLoading] = useState(!options.skip)
  const [error, setError] = useState<Error | null>(null)
  const paramsKey = JSON.stringify(params)
  const mounted = useRef(true)

  const run = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      await _waitForSchema()
      const res = await postDb<{ rows: TRow[] }>('query', {
        sql,
        params,
        scope: options.scope ?? 'all',
      })
      if (mounted.current) setData(res.rows)
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e : new Error(String(e)))
    } finally {
      if (mounted.current) setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sql, paramsKey, options.scope])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    if (!options.skip) run()
  }, [run, options.skip])

  return { data, loading, error, refetch: run }
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

/**
 * Declare the app's schema. On first mount the SDK runs it as a migration
 * (version 1). For evolving schemas, prefer numbered migrations applied by
 * an admin via the platform — but useAppSchema is the quick path for simple
 * apps. Idempotent: CREATE TABLE IF NOT EXISTS means re-runs are no-ops.
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
        await postDb('migrate', {
          migrations: [{ version: 1, name: 'app_schema', sql: schemaSql }],
        })
        setReady(true)
      } catch (e) {
        setError(e instanceof Error ? e : new Error(String(e)))
      } finally {
        _markSchemaSettled()
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return { ready, error }
}
