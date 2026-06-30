const BASE_URL = '/api'

interface RequestOptions {
  method?: string
  body?: unknown
  headers?: Record<string, string>
}

// Where we stash the access token so a browser refresh OR a newly-opened tab can
// recover the session instead of bouncing to /login. We use localStorage (shared
// across tabs) rather than sessionStorage (per-tab) specifically so that opening
// a menu item in a NEW TAB stays signed in — a fresh tab has empty sessionStorage,
// so it was landing on /login. The long-lived *refresh* token remains an httpOnly
// cookie (not readable by JS); only the short-lived access token lives here, an
// acceptable trade-off for an on-prem internal tool.
const TOKEN_STORAGE_KEY = 'aihub.accessToken'

function readStoredToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY)
  } catch {
    return null
  }
}

class ApiClient {
  private accessToken: string | null = null
  // Dedupe concurrent refreshes: parallel 401s share ONE /auth/refresh call.
  // Without this, simultaneous refreshes race and token rotation revokes all-but-
  // one — which itself bounces the user to /login.
  private refreshPromise: Promise<boolean> | null = null

  constructor() {
    this.accessToken = readStoredToken()
    // Mirror auth changes from other tabs (login/logout) so this tab doesn't keep
    // using a stale or cleared token.
    try {
      window.addEventListener('storage', (e) => {
        if (e.key === TOKEN_STORAGE_KEY) this.accessToken = e.newValue
      })
    } catch {
      // window/localStorage unavailable (SSR, sandbox) — in-memory token is fine.
    }
  }

  setToken(token: string | null) {
    this.accessToken = token
    try {
      if (token) {
        localStorage.setItem(TOKEN_STORAGE_KEY, token)
      } else {
        localStorage.removeItem(TOKEN_STORAGE_KEY)
      }
    } catch {
      // storage can throw in private-mode / sandboxed iframes — the in-memory
      // token still works for the current page load.
    }
  }

  getToken(): string | null {
    return this.accessToken
  }

  private async request<T>(endpoint: string, options: RequestOptions = {}): Promise<T> {
    const { method = 'GET', body, headers = {} } = options

    const reqHeaders: Record<string, string> = {
      'Content-Type': 'application/json',
      ...headers,
    }

    if (this.accessToken) {
      reqHeaders['Authorization'] = `Bearer ${this.accessToken}`
    }

    const response = await fetch(`${BASE_URL}${endpoint}`, {
      method,
      headers: reqHeaders,
      body: body ? JSON.stringify(body) : undefined,
      credentials: 'include',
    })

    if (response.status === 401) {
      // Try refresh
      const refreshed = await this.refreshToken()
      if (refreshed) {
        reqHeaders['Authorization'] = `Bearer ${this.accessToken}`
        const retryResponse = await fetch(`${BASE_URL}${endpoint}`, {
          method,
          headers: reqHeaders,
          body: body ? JSON.stringify(body) : undefined,
          credentials: 'include',
        })
        if (!retryResponse.ok) {
          throw new ApiError(retryResponse.status, await retryResponse.text())
        }
        return retryResponse.json()
      }
      this.accessToken = null
      throw new ApiError(401, 'Unauthorized')
    }

    if (!response.ok) {
      const errorText = await response.text()
      throw new ApiError(response.status, errorText)
    }

    if (response.status === 204) {
      return undefined as T
    }

    return response.json()
  }

  private refreshToken(): Promise<boolean> {
    // Share a single in-flight refresh across all concurrent callers.
    if (this.refreshPromise) return this.refreshPromise
    this.refreshPromise = (async () => {
      try {
        const response = await fetch(`${BASE_URL}/auth/refresh`, {
          method: 'POST',
          credentials: 'include',
        })
        if (response.ok) {
          const data = await response.json()
          this.setToken(data.access_token) // persist so new tabs / reloads keep it
          return true
        }
        return false
      } catch {
        return false
      } finally {
        this.refreshPromise = null
      }
    })()
    return this.refreshPromise
  }

  // Auth + 401-refresh-retry for non-JSON requests (file downloads/uploads).
  private async fetchWithAuth(endpoint: string, init: RequestInit): Promise<Response> {
    const doFetch = () => {
      const headers = new Headers(init.headers)
      if (this.accessToken) headers.set('Authorization', `Bearer ${this.accessToken}`)
      return fetch(`${BASE_URL}${endpoint}`, { ...init, headers, credentials: 'include' })
    }
    let response = await doFetch()
    if (response.status === 401 && (await this.refreshToken())) {
      response = await doFetch()
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text())
    }
    return response
  }

  /** GET a binary endpoint. Returns the blob plus the server-suggested filename. */
  async getBlob(endpoint: string): Promise<{ blob: Blob; filename: string | null }> {
    const response = await this.fetchWithAuth(endpoint, { method: 'GET' })
    const disposition = response.headers.get('content-disposition') ?? ''
    const match = /filename="?([^";]+)"?/.exec(disposition)
    return { blob: await response.blob(), filename: match ? match[1] : null }
  }

  /** POST multipart form data (browser sets the Content-Type boundary itself). */
  async postForm<T>(endpoint: string, form: FormData): Promise<T> {
    const response = await this.fetchWithAuth(endpoint, { method: 'POST', body: form })
    return response.json()
  }

  get<T>(endpoint: string) {
    return this.request<T>(endpoint)
  }

  post<T>(endpoint: string, body?: unknown) {
    return this.request<T>(endpoint, { method: 'POST', body })
  }

  put<T>(endpoint: string, body?: unknown) {
    return this.request<T>(endpoint, { method: 'PUT', body })
  }

  delete<T>(endpoint: string) {
    return this.request<T>(endpoint, { method: 'DELETE' })
  }
}

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

export const apiClient = new ApiClient()
