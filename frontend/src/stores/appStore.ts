import { create } from 'zustand'
import type { App } from '@/types'
import { apiClient } from '@/api/client'

interface AppState {
  apps: App[]
  currentApp: App | null
  isLoading: boolean
  fetchApps: () => Promise<void>
  createApp: (name: string, description?: string) => Promise<App>
  setCurrentApp: (app: App | null) => void
}

export const useAppStore = create<AppState>((set) => ({
  apps: [],
  currentApp: null,
  isLoading: false,

  fetchApps: async () => {
    set({ isLoading: true })
    try {
      const apps = await apiClient.get<App[]>('/apps')
      set({ apps, isLoading: false })
    } catch {
      set({ isLoading: false })
    }
  },

  createApp: async (name: string, description = '') => {
    const app = await apiClient.post<App>('/apps', { name, description })
    set((state) => ({ apps: [app, ...state.apps], currentApp: app }))
    return app
  },

  setCurrentApp: (app) => set({ currentApp: app }),
}))
