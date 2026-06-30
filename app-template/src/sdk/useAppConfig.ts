/**
 * useAppConfig — Access app settings/secrets at runtime.
 *
 * Resolution order:
 *   1. window.__AIHUB_CONFIG__   — injected by the local preview proxy.
 *   2. fetch ${AIHUB_BASE}/api/apps/{id}/settings/resolved
 *      where AIHUB_BASE comes from import.meta.env.VITE_AIHUB_BASE_URL,
 *      or same-origin when the env var is empty.
 */

import { useState, useEffect } from 'react'

declare global {
  interface Window {
    __AIHUB_CONFIG__?: Record<string, string>
    __AIHUB_APP_ID__?: string
  }
}

const AIHUB_BASE: string =
  ((import.meta as any).env?.VITE_AIHUB_BASE_URL as string | undefined) || ''

let cachedConfig: Record<string, string> | null = null

function getAppId(): string | null {
  if (window.__AIHUB_APP_ID__) return window.__AIHUB_APP_ID__
  const meta = document.querySelector('meta[name="aihub-app-id"]')
  if (meta) return meta.getAttribute('content')
  return null
}

async function loadConfig(): Promise<Record<string, string>> {
  if (window.__AIHUB_CONFIG__) {
    return window.__AIHUB_CONFIG__
  }

  const appId = getAppId()
  if (!appId) {
    console.warn('[AIHub SDK] No app ID found. Config will be empty.')
    return {}
  }

  try {
    const url = `${AIHUB_BASE}/api/apps/${appId}/settings/resolved`
    const response = await fetch(url, { credentials: 'include' })
    if (!response.ok) {
      console.error(`[AIHub SDK] Failed to load config: ${response.status}`)
      return {}
    }
    const config = await response.json()
    window.__AIHUB_CONFIG__ = config
    return config
  } catch (err) {
    console.error('[AIHub SDK] Error loading config:', err)
    return {}
  }
}

export function useAppConfig(): Record<string, string> {
  const [config, setConfig] = useState<Record<string, string>>(
    () => cachedConfig || window.__AIHUB_CONFIG__ || {}
  )

  useEffect(() => {
    if (cachedConfig) {
      setConfig(cachedConfig)
      return
    }

    loadConfig().then((cfg) => {
      cachedConfig = cfg
      setConfig(cfg)
    })
  }, [])

  return config
}
