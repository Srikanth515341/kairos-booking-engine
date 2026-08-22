import { createContext } from 'react'

export interface AuthUser {
  sub: string
  email?: string
  name?: string
}

// No 'loading' state: session restoration (AuthProvider) reads
// sessionStorage synchronously via a lazy useState initializer, so
// which of these two is true is already known on the very first render.
export type AuthStatus = 'authenticated' | 'unauthenticated'

export interface AuthContextValue {
  status: AuthStatus
  user: AuthUser | null
  /** Exchanges a raw OIDC ID token (from either dev-mock-login or a real
   * IdP redirect) for a backend session, persists it, and updates state. */
  loginWithIdToken: (idToken: string) => Promise<void>
  logout: () => void
}

// Kept in its own module (no JSX, no component) so AuthProvider.tsx and
// useAuth.ts can each export exactly one thing — satisfies eslint-plugin-
// react-refresh's "only export components" rule without disabling it.
export const AuthContext = createContext<AuthContextValue | null>(null)
