import { useState, type SubmitEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { devMockLogin } from '../api/auth'
import { ApiError, NetworkError } from '../api/errors'
import { beginOidcRedirect, isOidcConfigured } from './oidc'
import { useAuth } from './useAuth'

interface LocationState {
  from?: { pathname: string }
}

export function LoginPage() {
  const { loginWithIdToken } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [email, setEmail] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const redirectTo = (location.state as LocationState | null)?.from?.pathname ?? '/'

  async function handleDevLogin(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const { id_token: idToken } = await devMockLogin(email, displayName || undefined)
      await loginWithIdToken(idToken)
      await navigate(redirectTo, { replace: true })
    } catch (err) {
      if (err instanceof ApiError) {
        setError(`${err.message} (${err.code}${err.requestId ? `, request ${err.requestId}` : ''})`)
      } else if (err instanceof NetworkError) {
        setError(err.message)
      } else {
        setError('Sign-in failed unexpectedly.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50 px-4">
      <div className="w-full max-w-sm rounded-lg border border-gray-200 bg-white p-8 shadow-sm">
        <h1 className="mb-1 text-xl font-semibold text-gray-900">Sign in to Kairos</h1>
        <p className="mb-6 text-sm text-gray-500">Concurrency-safe resource booking.</p>

        <button
          type="button"
          onClick={beginOidcRedirect}
          disabled={!isOidcConfigured()}
          title={isOidcConfigured() ? undefined : 'No OIDC provider is configured for this build.'}
          className="mb-4 w-full rounded-md border border-gray-300 bg-white px-4 py-2 text-sm
            font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed
            disabled:opacity-50"
        >
          Sign in with SSO
        </button>

        <div className="mb-4 flex items-center gap-3 text-xs text-gray-400">
          <div className="h-px flex-1 bg-gray-200" />
          <span>dev sign-in</span>
          <div className="h-px flex-1 bg-gray-200" />
        </div>

        <form onSubmit={(e) => void handleDevLogin(e)} className="space-y-3">
          <div>
            <label htmlFor="email" className="mb-1 block text-sm font-medium text-gray-700">
              Email
            </label>
            <input
              id="email"
              type="email"
              required
              value={email}
              onChange={(e) => {
                setEmail(e.target.value)
              }}
              className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm
                focus:border-kairos-primary focus:outline-none focus:ring-1
                focus:ring-kairos-primary"
              placeholder="you@example.com"
            />
          </div>
          <div>
            <label htmlFor="displayName" className="mb-1 block text-sm font-medium text-gray-700">
              Display name (optional)
            </label>
            <input
              id="displayName"
              type="text"
              value={displayName}
              onChange={(e) => {
                setDisplayName(e.target.value)
              }}
              className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm
                focus:border-kairos-primary focus:outline-none focus:ring-1
                focus:ring-kairos-primary"
              placeholder="Jane Doe"
            />
          </div>

          {error && (
            <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-kairos-danger">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={submitting}
            className="w-full rounded-md bg-kairos-primary px-4 py-2 text-sm font-medium
              text-white hover:bg-kairos-primary-dark disabled:cursor-not-allowed
              disabled:opacity-60"
          >
            {submitting ? 'Signing in…' : 'Sign in (dev)'}
          </button>
        </form>
      </div>
    </div>
  )
}
