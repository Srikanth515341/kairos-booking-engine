import { apiClient } from './client'

export interface DevMockLoginResponse {
  id_token: string
}

/** `POST /auth/dev-mock-login` — dev/test only, mirrors `kairos.identity.
 * views.DevMockLoginView`. Mints an ID token against a fixed local
 * keypair standing in for a real IdP; disabled server-side (404) unless
 * `KAIROS_OIDC_MOCK_ENABLED` is set, so this call simply fails loudly in
 * an environment where it shouldn't be used at all. */
export function devMockLogin(email: string, displayName?: string): Promise<DevMockLoginResponse> {
  return apiClient.post<DevMockLoginResponse>('/auth/dev-mock-login', {
    email,
    display_name: displayName ?? null,
  })
}

export interface TokenExchangeResponse {
  access_token: string
  token_type: 'Bearer'
  expires_in: number
}

/** `POST /auth/token` — exchanges a real (or dev-mock) OIDC ID token for
 * this backend's own short-lived session token (RFC v1.0 §4). Every
 * subsequent authenticated request carries `access_token` as `Authorization:
 * Bearer <access_token>` — the ID token itself is used exactly once, here. */
export function exchangeToken(idToken: string): Promise<TokenExchangeResponse> {
  return apiClient.post<TokenExchangeResponse>('/auth/token', { id_token: idToken })
}
