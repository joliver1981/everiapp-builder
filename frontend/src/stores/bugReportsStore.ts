import { create } from 'zustand'

import { apiClient } from '@/api/client'
import type { BugReportDetail, BugReportSummary, FixAttempt } from '@/types'

interface BugReportsState {
  summaries: BugReportSummary[]
  isLoadingList: boolean
  details: Record<string, BugReportDetail>
  isLoadingDetail: Record<string, boolean>

  fetchList: (appId?: string) => Promise<void>
  fetchDetail: (id: string) => Promise<BugReportDetail | null>
  approve: (id: string, analysisId?: string) => Promise<FixAttempt | null>
  reject: (id: string, note: string) => Promise<void>
  reanalyze: (id: string, note: string) => Promise<void>
}

export const useBugReportsStore = create<BugReportsState>((set, get) => ({
  summaries: [],
  isLoadingList: false,
  details: {},
  isLoadingDetail: {},

  fetchList: async (appId) => {
    set({ isLoadingList: true })
    try {
      const url = appId ? `/bug-reports?app_id=${encodeURIComponent(appId)}` : '/bug-reports'
      const summaries = await apiClient.get<BugReportSummary[]>(url)
      set({ summaries, isLoadingList: false })
    } catch {
      set({ isLoadingList: false })
    }
  },

  fetchDetail: async (id) => {
    set((s) => ({ isLoadingDetail: { ...s.isLoadingDetail, [id]: true } }))
    try {
      const detail = await apiClient.get<BugReportDetail>(`/bug-reports/${id}`)
      set((s) => ({
        details: { ...s.details, [id]: detail },
        isLoadingDetail: { ...s.isLoadingDetail, [id]: false },
      }))
      return detail
    } catch {
      set((s) => ({ isLoadingDetail: { ...s.isLoadingDetail, [id]: false } }))
      return null
    }
  },

  approve: async (id, analysisId) => {
    const attempt = await apiClient.post<FixAttempt>(`/bug-reports/${id}/approve`, {
      analysis_id: analysisId ?? null,
    })
    await get().fetchDetail(id)
    await get().fetchList()
    return attempt
  },

  reject: async (id, note) => {
    await apiClient.post(`/bug-reports/${id}/reject`, { note })
    await get().fetchDetail(id)
    await get().fetchList()
  },

  reanalyze: async (id, note) => {
    await apiClient.post(`/bug-reports/${id}/reanalyze`, { note })
    await get().fetchDetail(id)
    await get().fetchList()
  },
}))
