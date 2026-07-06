/**
 * useAppConfig — Access app settings/secrets at runtime.
 *
 * Resolution order:
 *   1. window.__AIHUB_CONFIG__   — injected by the local preview proxy.
 *   2. fetch ${AIHUB_BASE}/api/apps/{id}/settings/resolved
 *      where AIHUB_BASE comes from import.meta.env.VITE_AIHUB_BASE_URL,
 *      or same-origin when the env var is empty.
 *
 * Auth: the resolved-settings endpoint is bearer-only (the platform never
 * sets an access-token cookie), so the fetch relies on window.__AIHUB_TOKEN__.
 * On-platform pages (builder Preview, app viewer) load apps through the
 * runtime proxy, which injects it. Deployed or embedded pages are not behind
 * that proxy: the host page must set window.__AIHUB_TOKEN__ before the app
 * mounts — otherwise config resolves to {}.
 */

import { useState, useEffect } from 'react'
import { hasSessionToken, notifySessionExpired } from './session'

declare global {
  interface Window {
    __AIHUB_CONFIG__?: Record<string, string>
    __AIHUB_APP_ID__?: string
    __AIHUB_TOKEN__?: string
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

/** Returns the config on success, or null on FAILURE (never cache failures). */
async function loadConfig(): Promise<Record<string, string> | null> {
  if (window.__AIHUB_CONFIG__) {
    return window.__AIHUB_CONFIG__
  }

  const appId = getAppId()
  if (!appId) {
    // Legitimately empty (not injected, no meta tag) — this cannot change
    // within a page's life, so treat as a successful empty config.
    console.warn('[AIHub SDK] No app ID found. Config will be empty.')
    return {}
  }

  try {
    const url = `${AIHUB_BASE}/api/apps/${appId}/settings/resolved`
    // The preview proxy injects the viewer's bearer token; the endpoint
    // requires auth (cookies alone don't carry it).
    const headers: Record<string, string> = {}
    if (window.__AIHUB_TOKEN__) {
      headers['Authorization'] = `Bearer ${window.__AIHUB_TOKEN__}`
    }
    const response = await fetch(url, { credentials: 'include', headers })
    if (!response.ok) {
      console.error(`[AIHub SDK] Failed to load config: ${response.status}`)
      if (response.status === 401 && hasSessionToken()) notifySessionExpired()
      return null
    }
    const config = await response.json()
    window.__AIHUB_CONFIG__ = config
    return config
  } catch (err) {
    console.error('[AIHub SDK] Error loading config:', err)
    return null
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
      // Cache ONLY success. Caching a failed ({}) load poisoned every later
      // mount for the page's lifetime — one transient 401/blip at boot and
      // settings-dependent features silently ran unconfigured until a full
      // reload. On failure the next mount simply retries.
      if (cfg !== null) {
        cachedConfig = cfg
      }
      setConfig(cfg ?? {})
    })
  }, [])

  return config
}
