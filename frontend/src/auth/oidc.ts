/**
 * Real-provider OIDC redirect (implicit flow: `response_type=id_token`).
 * Structurally complete but GENUINELY UNTESTED against a live IdP — the
 * identical documented-gap pattern the backend already carries for
 * `kairos.identity.oidc.verify_id_token`'s real-JWKS path ("structurally
 * complete but genuinely untested against a live IdP — this project has
 * none to test against"). Only `DevMockLoginPanel`
 * (`kairos.identity.views.DevMockLoginView`, dev/test only) is exercised
 * end-to-end.
 *
 * Implicit flow, not authorization-code+PKCE, is a deliberate scope
 * limitation, not an oversight: the backend's `POST /auth/token` expects
 * a raw `id_token` and performs no code-for-token exchange of its own
 * (`kairos/identity/views.py`'s `TokenExchangeView` — confirmed by
 * reading it), so a code-exchange flow would need the SPA to hold a
 * client secret (it can't, safely) or run its own PKCE exchange directly
 * against the IdP (real complexity with no live IdP here to verify it
 * against). Implicit flow produces exactly the `id_token` the backend
 * already knows how to verify, with no additional moving parts. This is
 * a real, known limitation (implicit flow is broadly discouraged by
 * current OIDC guidance) — flagged here and in CLAUDE.md's Open
 * Questions, not silently presented as production-grade.
 */

const OIDC_STATE_STORAGE_KEY = 'kairos.oidc.state'

export function isOidcConfigured(): boolean {
  return Boolean(import.meta.env.VITE_OIDC_ISSUER && import.meta.env.VITE_OIDC_CLIENT_ID)
}

/** Redirects the browser to the configured IdP's authorization endpoint.
 * Never called unless `isOidcConfigured()` is true — `LoginPage` gates
 * the button on it. */
export function beginOidcRedirect(): void {
  const issuer = import.meta.env.VITE_OIDC_ISSUER
  const clientId = import.meta.env.VITE_OIDC_CLIENT_ID
  if (!issuer || !clientId) {
    throw new Error('OIDC is not configured (VITE_OIDC_ISSUER / VITE_OIDC_CLIENT_ID unset).')
  }
  const redirectUri = `${window.location.origin}/auth/callback`
  const state = crypto.randomUUID()
  const nonce = crypto.randomUUID()
  // Verified on return (consumeOidcCallback) — the standard OIDC/OAuth2
  // CSRF mitigation: only a redirect THIS tab initiated will have the
  // matching value waiting in sessionStorage.
  sessionStorage.setItem(OIDC_STATE_STORAGE_KEY, state)

  const authorizeUrl = new URL(`${issuer.replace(/\/+$/, '')}/authorize`)
  authorizeUrl.searchParams.set('response_type', 'id_token')
  authorizeUrl.searchParams.set('client_id', clientId)
  authorizeUrl.searchParams.set('redirect_uri', redirectUri)
  authorizeUrl.searchParams.set('scope', 'openid email profile')
  authorizeUrl.searchParams.set('state', state)
  authorizeUrl.searchParams.set('nonce', nonce)
  authorizeUrl.searchParams.set('response_mode', 'fragment')

  window.location.assign(authorizeUrl.toString())
}

export interface OidcCallbackResult {
  idToken: string
}

/** Parses the URL fragment `OidcCallbackPage` receives after a redirect
 * back from the IdP. Throws a plain `Error` with a user-displayable
 * message on any failure (IdP-reported error, state mismatch, missing
 * token) — the caller renders it, it never reaches `ApiError` handling
 * since no backend call has happened yet at this point. */
export function consumeOidcCallback(locationHash: string): OidcCallbackResult {
  const params = new URLSearchParams(locationHash.replace(/^#/, ''))

  const idpError = params.get('error')
  if (idpError) {
    throw new Error(params.get('error_description') ?? idpError)
  }

  const expectedState = sessionStorage.getItem(OIDC_STATE_STORAGE_KEY)
  sessionStorage.removeItem(OIDC_STATE_STORAGE_KEY)
  const returnedState = params.get('state')
  if (!expectedState || returnedState !== expectedState) {
    throw new Error('Login could not be verified (state mismatch) — please try signing in again.')
  }

  const idToken = params.get('id_token')
  if (!idToken) {
    throw new Error('The identity provider did not return an ID token.')
  }
  return { idToken }
}
