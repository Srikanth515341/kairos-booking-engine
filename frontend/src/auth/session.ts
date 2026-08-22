/**
 * Session persistence — `sessionStorage`, not `localStorage`: the backend
 * issues a genuinely SHORT-LIVED session token (RFC v1.0 §4,
 * `KAIROS_SESSION_TOKEN_TTL_SECONDS`, 15 minutes by default) and
 * `sessionStorage`'s own "cleared when the tab closes" behavior is the
 * closer match to that lifetime — a token that outlives the tab in
 * `localStorage` would sit there long after it's actually expired.
 */

export interface PersistedSession {
  accessToken: string
  expiresAtMs: number
  user: {
    sub: string
    email?: string
    name?: string
  }
}

const STORAGE_KEY = 'kairos.session'

export function saveSession(session: PersistedSession): void {
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session))
}

export function loadSession(): PersistedSession | null {
  const raw = sessionStorage.getItem(STORAGE_KEY)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as PersistedSession
    if (parsed.expiresAtMs <= Date.now()) {
      clearSession()
      return null
    }
    return parsed
  } catch {
    clearSession()
    return null
  }
}

export function clearSession(): void {
  sessionStorage.removeItem(STORAGE_KEY)
}
