/**
 * Session-expiry handling shared by every SDK module that calls the platform.
 *
 * Apps receive a bearer token (window.__AIHUB_TOKEN__) injected at document
 * load. It is deliberately long-lived (preview sessions get ~12h), but there
 * is NO refresh path inside a running page — when it finally expires, every
 * platform call starts answering 401. Raw bodies like
 * `{"detail":"Invalid or expired token"}` rendered into app UI read as app
 * bugs, so instead:
 *   - surface ONE stable, human message (SESSION_EXPIRED_MESSAGE), and
 *   - dispatch a window event ('aihub:token-expired') so the host page or
 *     the app itself can react (e.g. prompt a reload, or re-mint a token).
 *
 * IMPORTANT: a 401 only means "session expired" when a token was actually
 * attached. A deployed/embedded app whose host never set __AIHUB_TOKEN__
 * (or sets it after mount, per the documented embed contract) also gets
 * 401s — telling that user to reload would be a lie (a reload still has no
 * token), so those keep the plain error shape.
 */

export const SESSION_EXPIRED_MESSAGE =
  'Session expired — reload the page to continue.'

/** Was a platform token available for the request that just failed? */
export function hasSessionToken(): boolean {
  try {
    return Boolean((window as any).__AIHUB_TOKEN__)
  } catch {
    return false
  }
}

// Expiry is unrecoverable within a page's life (nothing refreshes the
// injected token), so notify listeners ONCE — a dashboard with dozens of
// failing hooks must not storm the host with one event per fetch.
let notifiedExpiry = false

/** Broadcast that the platform rejected our token. Safe in any environment. */
export function notifySessionExpired(): void {
  if (notifiedExpiry) return
  notifiedExpiry = true
  try {
    window.dispatchEvent(new CustomEvent('aihub:token-expired'))
  } catch {
    /* non-browser environment — nothing to notify */
  }
}

/** The Error thrown to app code when a platform call came back 401. */
export function sessionExpiredError(): Error {
  notifySessionExpired()
  return new Error(SESSION_EXPIRED_MESSAGE)
}

/**
 * Build the Error for a failed platform response: the friendly session
 * message for 401s (when a token was actually attached — see above), the
 * standard `<what> failed (<status>): <body>` shape for everything else.
 */
export function platformError(what: string, status: number, bodyText: string): Error {
  if (status === 401 && hasSessionToken()) return sessionExpiredError()
  return new Error(`${what} failed (${status}): ${bodyText}`)
}
