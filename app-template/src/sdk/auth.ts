/**
 * AIHub App SDK - Auth Context
 *
 * Resolution order:
 *   1. window.__AIHUB_USER__ — injected by the local preview proxy.
 *   2. fetch ${AIHUB_BASE}/api/auth/me with credentials.
 */

const AIHUB_BASE: string =
  ((import.meta as any).env?.VITE_AIHUB_BASE_URL as string | undefined) || ''

export interface AppUser {
  id: string
  username: string
  display_name: string
  role: string
}

let cachedUser: AppUser | null | undefined = undefined

export function getUser(): AppUser | null {
  return (window as any).__AIHUB_USER__ || cachedUser || null
}

export async function fetchUser(): Promise<AppUser | null> {
  const injected = (window as any).__AIHUB_USER__
  if (injected) return injected
  if (cachedUser !== undefined) return cachedUser
  try {
    // /api/auth/me is BEARER-ONLY (it never reads cookies), so send the
    // injected token — 'credentials: include' alone always 401'd, which made
    // every deployed/embedded app see an anonymous user forever.
    const headers: Record<string, string> = {}
    const token = (window as any).__AIHUB_TOKEN__
    if (token) headers['Authorization'] = `Bearer ${token}`
    const resp = await fetch(`${AIHUB_BASE}/api/auth/me`, { credentials: 'include', headers })
    if (!resp.ok) {
      // Cache ONLY success: a host page may set window.__AIHUB_TOKEN__ after
      // mount (documented embed contract) — a cached null would blind
      // getUser()/fetchUser() for the rest of the page's life.
      return null
    }
    cachedUser = (await resp.json()) as AppUser
    return cachedUser
  } catch {
    return null
  }
}
