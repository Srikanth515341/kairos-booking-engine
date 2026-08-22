import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { exchangeToken } from '../api/auth'
import { setAccessToken, setUnauthorizedHandler } from '../api/client'
import { AuthContext, type AuthUser } from './context'
import { decodeJwtPayloadUnsafe } from './jwt'
import { clearSession, loadSession, saveSession, type PersistedSession } from './session'

function userFromSession(session: PersistedSession): AuthUser {
  return session.user
}

export function AuthProvider({ children }: { children: ReactNode }) {
  // Lazy initializer — runs exactly once, synchronously, before the
  // first paint, so a still-valid session (e.g. the tab was refreshed)
  // is reflected in the FIRST render. sessionStorage reads are
  // synchronous, so there is no genuine "loading" period to model here.
  const [session, setSession] = useState<PersistedSession | null>(() => loadSession())
  const status = session ? ('authenticated' as const) : ('unauthenticated' as const)
  const user = session ? userFromSession(session) : null

  const logout = useCallback(() => {
    clearSession()
    setSession(null)
  }, [])

  // Keeps the API client's module-level token in sync with React state —
  // synchronizing an external system, the textbook legitimate use of an
  // effect, and never itself a setState call (so this doesn't trip
  // react-hooks/set-state-in-effect the way doing this at mount inline
  // used to).
  useEffect(() => {
    setAccessToken(session?.accessToken ?? null)
  }, [session])

  // Wires the API client's 401 hook exactly once — any request coming
  // back `unauthorized` logs the app out centrally, not just ones a
  // particular component happens to check for.
  useEffect(() => {
    setUnauthorizedHandler(logout)
    return () => {
      setUnauthorizedHandler(null)
    }
  }, [logout])

  const loginWithIdToken = useCallback(async (idToken: string) => {
    const claims = decodeJwtPayloadUnsafe(idToken)
    const tokenResponse = await exchangeToken(idToken)
    const newSession: PersistedSession = {
      accessToken: tokenResponse.access_token,
      expiresAtMs: Date.now() + tokenResponse.expires_in * 1000,
      user: {
        sub: claims?.sub ?? 'unknown',
        ...(claims?.email !== undefined && { email: claims.email }),
        ...(claims?.name !== undefined && { name: claims.name }),
      },
    }
    saveSession(newSession)
    setSession(newSession)
  }, [])

  const value = useMemo(
    () => ({ status, user, loginWithIdToken, logout }),
    [status, user, loginWithIdToken, logout],
  )

  return <AuthContext value={value}>{children}</AuthContext>
}
