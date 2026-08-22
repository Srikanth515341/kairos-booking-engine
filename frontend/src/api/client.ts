import {
  API_BASE_URL,
  SERVICE_UNAVAILABLE_DEFAULT_RETRY_AFTER_SECONDS,
  SERVICE_UNAVAILABLE_MAX_RETRIES,
} from './config'
import { ApiError, NetworkError, type ApiErrorBody } from './errors'

type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'DELETE'

export interface RequestOptions {
  /**
   * Only meaningful for mutating methods (POST/PATCH/DELETE). If omitted,
   * one UUID is generated automatically — exactly once per call to this
   * function, held fixed across every automatic retry that call performs
   * (Spec v1.0 §10: "generate the Idempotency-Key once per user action
   * and reuse it across every automatic retry"). Pass this explicitly
   * only when the CALLER is the one re-issuing a genuinely new user
   * action (e.g. the user dismissed an error and clicked "Book" again) —
   * that deserves a fresh key, because it's a new action, not a retry of
   * the old one.
   */
  idempotencyKey?: string
  signal?: AbortSignal
}

let accessToken: string | null = null
let unauthorizedHandler: (() => void) | null = null

/** Called by the auth layer after login/logout — the client never reads
 * session storage itself, so it has exactly one source of truth for
 * "what bearer token, if any, is current." */
export function setAccessToken(token: string | null): void {
  accessToken = token
}

/** Called once at app startup by the auth layer. Fired whenever ANY
 * request comes back `unauthorized` — the session is gone (expired,
 * revoked, or never existed) regardless of which call noticed first. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

function generateId(): string {
  return crypto.randomUUID()
}

type SleepFn = (ms: number) => Promise<void>

const defaultSleep: SleepFn = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

// Overridable only from tests (client.test.ts) so a 503-retry test
// doesn't actually block on real wall-clock delays. Never touched by
// application code.
let sleepImpl: SleepFn = defaultSleep
export function __setSleepImplForTesting(fn: SleepFn | null): void {
  sleepImpl = fn ?? defaultSleep
}

async function performFetch(
  method: HttpMethod,
  path: string,
  body: unknown,
  idempotencyKey: string | undefined,
  signal: AbortSignal | undefined,
): Promise<{ response: Response; requestId: string }> {
  // A fresh X-Request-Id per HTTP ATTEMPT, not per logical action — this
  // is deliberately the opposite lifetime from idempotencyKey above.
  // request_id is a per-request audit/tracing correlator on the server
  // (kairos.core.middleware.RequestIdMiddleware attaches it to that
  // one request's audit rows and structured logs); Idempotency-Key is
  // what makes retries of the SAME action safe to collapse into one
  // outcome. Conflating the two would either replay a stale trace id
  // onto a genuinely new server-side attempt, or mint a fresh
  // idempotency key per attempt and break the retry contract — so they
  // must vary independently.
  const requestId = generateId()
  const headers: Record<string, string> = { 'X-Request-Id': requestId }
  if (accessToken) {
    headers.Authorization = `Bearer ${accessToken}`
  }

  let requestBody: string | undefined
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    requestBody = JSON.stringify(body)
  }
  if (idempotencyKey) {
    headers['Idempotency-Key'] = idempotencyKey
  }

  const init: RequestInit = {
    method,
    headers,
    body: requestBody ?? null,
  }
  if (signal) {
    init.signal = signal
  }

  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, init)
  } catch (cause) {
    throw new NetworkError(cause)
  }
  return { response, requestId }
}

async function parseErrorBody(response: Response, requestId: string): Promise<ApiErrorBody> {
  try {
    const data = (await response.json()) as { error?: Partial<ApiErrorBody> }
    if (data.error && typeof data.error.code === 'string') {
      return {
        code: data.error.code,
        message: data.error.message ?? 'The request failed.',
        details: data.error.details ?? {},
        request_id: data.error.request_id ?? requestId,
      }
    }
  } catch {
    // Body wasn't JSON, or wasn't the Spec v1.0 §6 envelope shape — fall
    // through to the synthetic body below rather than propagate a raw
    // parse error that would hide the real HTTP status from the caller.
  }
  return {
    code: 'internal_error',
    message: `Request failed with status ${String(response.status)}.`,
    details: {},
    request_id: requestId,
  }
}

function retryAfterSecondsFrom(response: Response): number | null {
  const header = response.headers.get('Retry-After')
  if (!header) return null
  const parsed = Number(header)
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null
}

async function request<T>(
  method: HttpMethod,
  path: string,
  body: unknown,
  options: RequestOptions = {},
): Promise<T> {
  const isMutating = method !== 'GET'
  // Generated ONCE, here — outside the retry loop below. This single
  // line IS the idempotency contract (Spec v1.0 §10): every automatic
  // retry this function performs for THIS call reuses this exact value.
  // A version of this function that generated a key inside the `for`
  // loop would silently defeat the entire contract while still
  // "working" on the happy path, which is exactly why this lives here
  // and is unit-tested directly (client.test.ts).
  const idempotencyKey = isMutating ? (options.idempotencyKey ?? generateId()) : undefined

  let attempt = 0
  for (;;) {
    attempt += 1
    const { response, requestId } = await performFetch(
      method,
      path,
      body,
      idempotencyKey,
      options.signal,
    )

    if (response.ok) {
      if (response.status === 204) {
        return undefined as T
      }
      return (await response.json()) as T
    }

    const errorBody = await parseErrorBody(response, requestId)
    const retryAfterSeconds = retryAfterSecondsFrom(response)
    const error = new ApiError(errorBody, response.status, retryAfterSeconds)

    if (error.code === 'unauthorized') {
      unauthorizedHandler?.()
    }

    // Spec v1.0 §10 / PRD M14: 503 means "the outcome is UNKNOWN, retry
    // with the same key" — never "the booking failed." Retried
    // automatically and transparently, up to a bound, using the SAME
    // idempotencyKey computed once above. The backend's own Retry-After
    // header is honored when present (kairos.core.exceptions.
    // ServiceUnavailableError always sets one); a fixed default covers
    // the case where it's somehow absent.
    if (error.code === 'service_unavailable' && attempt <= SERVICE_UNAVAILABLE_MAX_RETRIES) {
      const waitSeconds = error.retryAfterSeconds ?? SERVICE_UNAVAILABLE_DEFAULT_RETRY_AFTER_SECONDS
      await sleepImpl(waitSeconds * 1000)
      continue
    }

    throw error
  }
}

export const apiClient = {
  get: <T>(path: string, options?: RequestOptions): Promise<T> =>
    request<T>('GET', path, undefined, options),
  post: <T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> =>
    request<T>('POST', path, body, options),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> =>
    request<T>('PATCH', path, body, options),
  delete: <T>(path: string, options?: RequestOptions): Promise<T> =>
    request<T>('DELETE', path, undefined, options),
}
