import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { consumeOidcCallback } from './oidc'
import { useAuth } from './useAuth'

/** Route target for the real-provider redirect (`/auth/callback`) — see
 * `oidc.ts`'s module docstring for why this path is structurally
 * complete but untested against a live IdP. */
export function OidcCallbackPage() {
  const { loginWithIdToken } = useAuth()
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    async function run() {
      try {
        const { idToken } = consumeOidcCallback(window.location.hash)
        await loginWithIdToken(idToken)
        if (!cancelled) {
          window.location.replace('/')
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Sign-in failed.')
        }
      }
    }
    void run()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runs once, against the redirect URL present at mount
  }, [])

  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-50 px-4">
        <div className="max-w-sm text-center">
          <p role="alert" className="mb-4 text-sm text-kairos-danger">
            {error}
          </p>
          <Link to="/login" className="text-sm text-kairos-primary hover:underline">
            Back to sign in
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <p className="text-sm text-gray-500">Signing in…</p>
    </div>
  )
}
