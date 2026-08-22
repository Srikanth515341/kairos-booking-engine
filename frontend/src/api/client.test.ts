import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { API_BASE_URL } from './config'
import {
  __setSleepImplForTesting,
  apiClient,
  setAccessToken,
  setUnauthorizedHandler,
} from './client'
import { ApiError } from './errors'

function jsonResponse(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

function errorEnvelope(code: string, message = 'error', requestId: string | null = 'srv-req-id') {
  return { error: { code, message, details: {}, request_id: requestId } }
}

describe('apiClient', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    // Every retry-loop test needs sleeps to resolve instantly — real
    // `setTimeout` delays would make this suite slow without proving
    // anything more than the same behavior.
    __setSleepImplForTesting(() => Promise.resolve())
    setAccessToken(null)
    setUnauthorizedHandler(null)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    __setSleepImplForTesting(null)
  })

  // ------------------------------------------------------------------
  // Idempotency-Key: one per action, reused across every automatic retry
  // ------------------------------------------------------------------

  it('generates exactly one Idempotency-Key for a POST and sends it unchanged on every retry', async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(503, errorEnvelope('service_unavailable'), { 'Retry-After': '0' }),
      )
      .mockResolvedValueOnce(
        jsonResponse(503, errorEnvelope('service_unavailable'), { 'Retry-After': '0' }),
      )
      .mockResolvedValueOnce(jsonResponse(201, { id: 'booking-1' }))

    await apiClient.post('/bookings', { resource_id: 'r1' })

    expect(fetchMock).toHaveBeenCalledTimes(3)
    const idempotencyKeys = fetchMock.mock.calls.map(([, init]) => {
      const headers = (init as RequestInit).headers as Record<string, string>
      return headers['Idempotency-Key']
    })

    // Same key on every attempt...
    expect(idempotencyKeys[0]).toBeDefined()
    expect(idempotencyKeys[1]).toBe(idempotencyKeys[0])
    expect(idempotencyKeys[2]).toBe(idempotencyKeys[0])
    // ...and it looks like a real UUID, not an empty/placeholder value.
    expect(idempotencyKeys[0]).toMatch(/^[0-9a-f-]{36}$/i)
  })

  it('generates a DIFFERENT Idempotency-Key for a separate, later call — retries reuse one key, new actions do not', async () => {
    // A fresh Response per invocation — a Response's body can only be
    // read (`.json()`) once, so reusing one instance across two separate
    // apiClient.post() calls would fail the second read.
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse(201, { id: 'booking-1' })))

    await apiClient.post('/bookings', { resource_id: 'r1' })
    await apiClient.post('/bookings', { resource_id: 'r1' })

    const [firstCall, secondCall] = fetchMock.mock.calls
    const firstKey = ((firstCall?.[1] as RequestInit).headers as Record<string, string>)[
      'Idempotency-Key'
    ]
    const secondKey = ((secondCall?.[1] as RequestInit).headers as Record<string, string>)[
      'Idempotency-Key'
    ]
    expect(firstKey).not.toBe(secondKey)
  })

  it('never sends an Idempotency-Key on GET requests', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { data: [] }))

    await apiClient.get('/resources')

    const headers = (fetchMock.mock.calls[0]?.[1] as RequestInit).headers as Record<string, string>
    expect(headers['Idempotency-Key']).toBeUndefined()
  })

  it('honors an explicitly-provided idempotencyKey instead of generating one', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, { id: 'booking-1' }))

    await apiClient.post('/bookings', { resource_id: 'r1' }, { idempotencyKey: 'my-fixed-key' })

    const headers = (fetchMock.mock.calls[0]?.[1] as RequestInit).headers as Record<string, string>
    expect(headers['Idempotency-Key']).toBe('my-fixed-key')
  })

  // ------------------------------------------------------------------
  // X-Request-Id: fresh per HTTP attempt (independent lifetime from the
  // Idempotency-Key above)
  // ------------------------------------------------------------------

  it('sends a fresh X-Request-Id on every retry attempt, unlike the Idempotency-Key', async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(503, errorEnvelope('service_unavailable'), { 'Retry-After': '0' }),
      )
      .mockResolvedValueOnce(jsonResponse(201, { id: 'booking-1' }))

    await apiClient.post('/bookings', { resource_id: 'r1' })

    const requestIds = fetchMock.mock.calls.map(([, init]) => {
      const headers = (init as RequestInit).headers as Record<string, string>
      return headers['X-Request-Id']
    })
    expect(requestIds[0]).toBeDefined()
    expect(requestIds[1]).toBeDefined()
    expect(requestIds[1]).not.toBe(requestIds[0])
  })

  // ------------------------------------------------------------------
  // 503 = "unknown, retry" — never surfaced as an immediate failure
  // ------------------------------------------------------------------

  it('retries transparently on 503 and returns the eventual success — never throwing for the intermediate 503s', async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(503, errorEnvelope('service_unavailable'), { 'Retry-After': '2' }),
      )
      .mockResolvedValueOnce(jsonResponse(201, { id: 'booking-1', status: 'confirmed' }))

    const result = await apiClient.post<{ id: string }>('/bookings', { resource_id: 'r1' })

    expect(result).toEqual({ id: 'booking-1', status: 'confirmed' })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('waits for the server-specified Retry-After before retrying a 503', async () => {
    const sleepSpy = vi.fn(() => Promise.resolve())
    __setSleepImplForTesting(sleepSpy)
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(503, errorEnvelope('service_unavailable'), { 'Retry-After': '7' }),
      )
      .mockResolvedValueOnce(jsonResponse(201, { id: 'booking-1' }))

    await apiClient.post('/bookings', { resource_id: 'r1' })

    expect(sleepSpy).toHaveBeenCalledWith(7000)
  })

  it('gives up after the retry bound and throws a service_unavailable ApiError, not a generic failure', async () => {
    // A fresh Response per attempt — see the note in the previous test.
    fetchMock.mockImplementation(() =>
      Promise.resolve(
        jsonResponse(503, errorEnvelope('service_unavailable', 'still unknown'), {
          'Retry-After': '0',
        }),
      ),
    )

    await expect(apiClient.post('/bookings', { resource_id: 'r1' })).rejects.toMatchObject({
      code: 'service_unavailable',
    })
    // Bounded — not an infinite loop.
    expect(fetchMock.mock.calls.length).toBeGreaterThan(1)
    expect(fetchMock.mock.calls.length).toBeLessThan(10)
  })

  it('retries using the identical Idempotency-Key across a long 503 streak, right up to the final attempt', async () => {
    // A fresh Response per attempt — see the note two tests up. Without
    // this, only the FIRST attempt's response is ever readable; every
    // subsequent attempt's `.json()` throws "body already read," which
    // this file's earlier bug (now fixed the same way) silently masked
    // as an `internal_error` that stopped the retry loop after one
    // retry — defeating the entire point of this specific test.
    fetchMock.mockImplementation(() =>
      Promise.resolve(
        jsonResponse(503, errorEnvelope('service_unavailable'), { 'Retry-After': '0' }),
      ),
    )

    await expect(apiClient.post('/bookings', { resource_id: 'r1' })).rejects.toBeInstanceOf(
      ApiError,
    )

    const keys = new Set(
      fetchMock.mock.calls.map(
        ([, init]) => ((init as RequestInit).headers as Record<string, string>)['Idempotency-Key'],
      ),
    )
    expect(keys.size).toBe(1)
  })

  // ------------------------------------------------------------------
  // Error handling branches on error.code, never on HTTP status alone
  // ------------------------------------------------------------------

  it('distinguishes request_in_progress from slot_unavailable even though both are HTTP 409', async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(409, errorEnvelope('request_in_progress', 'still running')),
      )
      .mockResolvedValueOnce(jsonResponse(409, errorEnvelope('slot_unavailable', 'taken')))

    const first = await apiClient
      .post('/bookings', { resource_id: 'r1' })
      .catch((err: unknown) => err)
    const second = await apiClient
      .post('/bookings', { resource_id: 'r1' })
      .catch((err: unknown) => err)

    expect(first).toBeInstanceOf(ApiError)
    expect(second).toBeInstanceOf(ApiError)
    expect((first as ApiError).status).toBe(409)
    expect((second as ApiError).status).toBe(409)
    // The status is identical; the CODE is what a caller must branch on.
    expect((first as ApiError).code).toBe('request_in_progress')
    expect((second as ApiError).code).toBe('slot_unavailable')
    expect((first as ApiError).code).not.toBe((second as ApiError).code)
  })

  it('carries details, message, and request_id through onto the thrown ApiError', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(400, {
        error: {
          code: 'validation_error',
          message: 'The request failed validation.',
          details: { field: 'end', issue: 'must be after start' },
          request_id: 'req-abc',
        },
      }),
    )

    let error: ApiError | undefined
    try {
      await apiClient.post('/bookings', { resource_id: 'r1' })
    } catch (err) {
      error = err as ApiError
    }

    expect(error).toBeDefined()
    expect(error?.code).toBe('validation_error')
    expect(error?.details).toEqual({ field: 'end', issue: 'must be after start' })
    expect(error?.requestId).toBe('req-abc')
  })

  it('invokes the unauthorized handler when a request comes back 401 unauthorized', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    fetchMock.mockResolvedValueOnce(
      jsonResponse(401, errorEnvelope('unauthorized', 'session expired')),
    )

    await expect(apiClient.get('/resources')).rejects.toMatchObject({ code: 'unauthorized' })
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('does not invoke the unauthorized handler for an unrelated error code', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    fetchMock.mockResolvedValueOnce(jsonResponse(409, errorEnvelope('slot_unavailable')))

    await expect(apiClient.post('/bookings', {})).rejects.toMatchObject({
      code: 'slot_unavailable',
    })
    expect(handler).not.toHaveBeenCalled()
  })

  // ------------------------------------------------------------------
  // Plumbing: base URL, auth header, success parsing
  // ------------------------------------------------------------------

  it('sends the Authorization header once a session token is set', async () => {
    setAccessToken('token-123')
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { data: [] }))

    await apiClient.get('/resources')

    const headers = (fetchMock.mock.calls[0]?.[1] as RequestInit).headers as Record<string, string>
    expect(headers.Authorization).toBe('Bearer token-123')
  })

  it('omits the Authorization header when no session token is set', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { data: [] }))

    await apiClient.get('/resources')

    const headers = (fetchMock.mock.calls[0]?.[1] as RequestInit).headers as Record<string, string>
    expect(headers.Authorization).toBeUndefined()
  })

  it('requests against the configured API base URL', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { data: [] }))

    await apiClient.get('/resources')

    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${API_BASE_URL}/resources`)
  })
})
