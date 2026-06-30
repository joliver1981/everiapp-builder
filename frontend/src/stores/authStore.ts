import { create } from 'zustand'
import type { User } from '@/types'
import { apiClient } from '@/api/client'

interface AuthState {
  user: User | null
  isAuthenticated: boolean
  isLoading: boolean
  isCheckingAuth: boolean
  login: (username: string, password: string) => Promise<void>
  loginWithToken: (token: string) => Promise<void>
  logout: () => Promise<void>
  checkAuth: () => Promise<void>
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  isAuthenticated: false,
  isLoading: true,
  isCheckingAuth: false,

  login: async (username: string, password: string) => {
    const data = await apiClient.post<{ access_token: string; user: User }>(
      '/auth/login',
      { username, password }
    )
    apiClient.setToken(data.access_token)
    set({ user: data.user, isAuthenticated: true })
  },

  // Adopt an access token minted by an external flow (e.g. the SAML ACS
  // redirect lands on /login#access_token=...). Loads the user via /auth/me.
  loginWithToken: async (token: string) => {
    apiClient.setToken(token)
    const data = await apiClient.get<{ user: User }>('/auth/me')
    set({ user: data.user, isAuthenticated: true, isLoading: false })
  },

  logout: async () => {
    try {
      await apiClient.post('/auth/logout')
    } finally {
      apiClient.setToken(null)
      set({ user: null, isAuthenticated: false })
    }
  },

  checkAuth: async () => {
    // Prevent duplicate auth checks
    const { isCheckingAuth } = get()
    if (isCheckingAuth) return

    set({ isCheckingAuth: true })
    try {
      // /auth/me uses the in-memory or sessionStorage-restored access token.
      // If missing/expired, apiClient transparently calls /auth/refresh
      // (httpOnly cookie) and retries — so a hard refresh recovers the session
      // instead of bouncing to /login.
      const data = await apiClient.get<{ user: User }>('/auth/me')
      set({ user: data.user, isAuthenticated: true, isLoading: false, isCheckingAuth: false })
    } catch {
      apiClient.setToken(null)
      set({ user: null, isAuthenticated: false, isLoading: false, isCheckingAuth: false })
    }
  },
}))
