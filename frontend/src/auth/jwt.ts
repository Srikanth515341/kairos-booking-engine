/**
 * Decodes a JWT's payload WITHOUT verifying its signature — used only to
 * pull display fields (email/name) out of an ID token client-side for
 * the UI to show immediately after login. This is never used for any
 * authorization decision: the backend independently re-verifies the same
 * ID token's signature when the frontend hands it to `POST /auth/token`
 * (`kairos.identity.oidc.verify_id_token`), and every subsequent API
 * call authenticates with the backend's own session token, not this one.
 * A forged/tampered ID token would simply be rejected there — decoding
 * it here first changes nothing about that guarantee.
 */
export interface DecodedIdToken {
  sub?: string
  email?: string
  name?: string
  exp?: number
  [claim: string]: unknown
}

export function decodeJwtPayloadUnsafe(token: string): DecodedIdToken | null {
  const parts = token.split('.')
  if (parts.length !== 3) return null
  const payload = parts[1]
  if (!payload) return null
  try {
    const base64 = payload.replace(/-/g, '+').replace(/_/g, '/')
    const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), '=')
    const json = atob(padded)
    return JSON.parse(json) as DecodedIdToken
  } catch {
    return null
  }
}
