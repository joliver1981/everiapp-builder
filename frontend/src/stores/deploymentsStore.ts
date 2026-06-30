import { create } from 'zustand'

import { apiClient } from '@/api/client'
import type { Deployment, DeploymentTarget, TargetTestResult } from '@/types'

interface DeploymentsState {
  targets: DeploymentTarget[]
  isLoadingTargets: boolean
  deploymentsByApp: Record<string, Deployment[]>
  isLoadingDeployments: Record<string, boolean>

  fetchTargets: () => Promise<void>
  createTarget: (data: TargetCreatePayload) => Promise<DeploymentTarget>
  updateTarget: (id: string, data: Partial<TargetCreatePayload>) => Promise<DeploymentTarget>
  deleteTarget: (id: string) => Promise<void>
  testTarget: (id: string) => Promise<TargetTestResult>

  fetchDeployments: (appId: string) => Promise<void>
  deployVersion: (appId: string, version: number, targetId: string) => Promise<Deployment>
  stopDeployment: (deploymentId: string, appId: string) => Promise<void>
  redeploy: (deploymentId: string, appId: string) => Promise<Deployment>
  fetchLogs: (deploymentId: string, n?: number) => Promise<string[]>
}

export interface TargetCreatePayload {
  name: string
  kind: 'agent' | 'ssh'
  host: string
  port: number
  ssh_user?: string | null
  port_range_start: number
  port_range_end: number
  environment: string
  credential_secret_id?: string | null
  extra_config?: Record<string, unknown>
  is_active: boolean
}

export const useDeploymentsStore = create<DeploymentsState>((set, get) => ({
  targets: [],
  isLoadingTargets: false,
  deploymentsByApp: {},
  isLoadingDeployments: {},

  fetchTargets: async () => {
    set({ isLoadingTargets: true })
    try {
      const targets = await apiClient.get<DeploymentTarget[]>('/admin/deployment-targets')
      set({ targets, isLoadingTargets: false })
    } catch {
      set({ isLoadingTargets: false })
    }
  },

  createTarget: async (data) => {
    const target = await apiClient.post<DeploymentTarget>('/admin/deployment-targets', data)
    set((s) => ({ targets: [...s.targets, target] }))
    return target
  },

  updateTarget: async (id, data) => {
    const target = await apiClient.put<DeploymentTarget>(`/admin/deployment-targets/${id}`, data)
    set((s) => ({ targets: s.targets.map((t) => (t.id === id ? target : t)) }))
    return target
  },

  deleteTarget: async (id) => {
    await apiClient.delete(`/admin/deployment-targets/${id}`)
    set((s) => ({ targets: s.targets.filter((t) => t.id !== id) }))
  },

  testTarget: async (id) => {
    const result = await apiClient.post<TargetTestResult>(
      `/admin/deployment-targets/${id}/test`,
    )
    // Refresh the target row so last_seen_at / agent_version reflect the test.
    await get().fetchTargets()
    return result
  },

  fetchDeployments: async (appId) => {
    set((s) => ({ isLoadingDeployments: { ...s.isLoadingDeployments, [appId]: true } }))
    try {
      const deployments = await apiClient.get<Deployment[]>(`/apps/${appId}/deployments`)
      set((s) => ({
        deploymentsByApp: { ...s.deploymentsByApp, [appId]: deployments },
        isLoadingDeployments: { ...s.isLoadingDeployments, [appId]: false },
      }))
    } catch {
      set((s) => ({
        isLoadingDeployments: { ...s.isLoadingDeployments, [appId]: false },
      }))
    }
  },

  deployVersion: async (appId, version, targetId) => {
    const deployment = await apiClient.post<Deployment>(
      `/apps/${appId}/versions/${version}/deploy`,
      { target_id: targetId },
    )
    await get().fetchDeployments(appId)
    return deployment
  },

  stopDeployment: async (deploymentId, appId) => {
    await apiClient.post(`/deployments/${deploymentId}/stop`)
    await get().fetchDeployments(appId)
  },

  redeploy: async (deploymentId, appId) => {
    const deployment = await apiClient.post<Deployment>(
      `/deployments/${deploymentId}/redeploy`,
    )
    await get().fetchDeployments(appId)
    return deployment
  },

  fetchLogs: async (deploymentId, n = 200) => {
    const result = await apiClient.get<{ lines: string[] }>(
      `/deployments/${deploymentId}/logs?n=${n}`,
    )
    return result.lines
  },
}))
