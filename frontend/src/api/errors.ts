/**
 * Every error code the backend's Spec v1.0 §6 envelope can carry
 * (`kairos/core/drf.py`, `kairos/core/exceptions.py`, `kairos/core/
 * error_handlers.py` — confirmed by reading the real backend source, not
 * guessed from the API spec doc alone). Kept as a literal union, not a
 * loose `string`, so a missing `case` in a `switch (error.code)` is a
 * compile error, not a silent fallthrough.
 */
export type ApiErrorCode =
  | 'validation_error'
  | 'unauthorized'
  | 'permission_denied'
  | 'not_found'
  | 'slot_unavailable'
  | 'already_on_waitlist'
  | 'offer_expired'
  | 'offer_already_resolved'
  | 'request_in_progress'
  | 'preview_expired'
  | 'unacknowledged_conflicts'
  | 'already_resource_admin'
  | 'slot_already_available'
  | 'idempotency_key_conflict'
  | 'rate_limited'
  | 'service_unavailable'
  | 'internal_error'

export interface ApiErrorBody {
  // `(string & {})` deliberately, not a bare `string`: it keeps the
  // literal union's autocomplete/exhaustiveness-checking usable at
  // call sites (`switch (error.code)` still suggests every known code)
  // while still accepting a code the backend adds later that this
  // frontend build doesn't know about yet — a bare `ApiErrorCode |
  // string` would collapse to plain `string` and lose that entirely
  // (the exact thing `@typescript-eslint/no-redundant-type-constituents`
  // flags).
  code: ApiErrorCode | (string & {})
  message: string
  details: Record<string, unknown>
  request_id: string | null
}

/**
 * Thrown by the API client for every non-2xx response — always carries a
 * real, structured `code`, never just an HTTP status. Consumers MUST
 * branch on `.code`, not `.status`: `request_in_progress` and
 * `slot_unavailable` are both 409 with opposite meanings (Spec v1.0 §10),
 * and collapsing them to "the request was a 409" loses the one bit of
 * information that actually matters.
 */
export class ApiError extends Error {
  // See ApiErrorBody.code's own comment for why `(string & {})`, not `string`.
  readonly code: ApiErrorCode | (string & {})
  readonly details: Record<string, unknown>
  readonly status: number
  /** The `X-Request-Id` of the attempt that produced THIS error — surface
   * it to the user ("quote this to support") per Spec v1.0 §10. */
  readonly requestId: string | null
  /** Present only for a genuine `service_unavailable` (503) — the
   * server's own hint for how long to wait before retrying. */
  readonly retryAfterSeconds: number | null

  constructor(body: ApiErrorBody, status: number, retryAfterSeconds: number | null = null) {
    super(body.message)
    this.name = 'ApiError'
    this.code = body.code
    this.details = body.details
    this.status = status
    this.requestId = body.request_id
    this.retryAfterSeconds = retryAfterSeconds
  }
}

/**
 * A network-level failure (fetch itself threw — DNS, connection refused,
 * CORS, the tab going offline mid-request) as opposed to a server
 * response we could parse. Deliberately a SEPARATE type from `ApiError`:
 * there is no `error.code` here because the server was never reached (or
 * its response never arrived), so there's nothing to branch on.
 */
export class NetworkError extends Error {
  readonly cause: unknown
  constructor(cause: unknown) {
    super('The request could not be sent — check your connection and try again.')
    this.name = 'NetworkError'
    this.cause = cause
  }
}
