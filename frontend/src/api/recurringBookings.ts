import { apiClient } from './client'
import type {
  RecurringConfirmRequest,
  RecurringConfirmResponse,
  RecurringPreviewRequest,
  RecurringPreviewResponse,
  RecurringSeriesCancelResponse,
} from './types'

/** `POST /bookings/recurring/preview` — commits nothing (no Idempotency-Key
 * involved server-side; the client still sends one automatically, the
 * backend simply never looks at it here). */
export function previewRecurringSeries(
  input: RecurringPreviewRequest,
): Promise<RecurringPreviewResponse> {
  return apiClient.post<RecurringPreviewResponse>('/bookings/recurring/preview', input)
}

/** `POST /bookings/recurring` — real HTTP 207 Multi-Status on success
 * (per-occurrence outcomes, not one pass/fail); `fetch`'s own
 * `Response.ok` already treats 207 as a 2xx success, so this resolves
 * normally rather than throwing. */
export function confirmRecurringSeries(
  input: RecurringConfirmRequest,
): Promise<RecurringConfirmResponse> {
  return apiClient.post<RecurringConfirmResponse>('/bookings/recurring', input)
}

/** `POST /recurring-series/{id}/cancel` — cancels every still-confirmed
 * FUTURE occurrence only; past occurrences remain as historical records
 * (Spec v1.0 §5.10, PRD FR15). */
export function cancelRecurringSeries(
  seriesId: string,
  reason?: string,
): Promise<RecurringSeriesCancelResponse> {
  return apiClient.post<RecurringSeriesCancelResponse>(`/recurring-series/${seriesId}/cancel`, {
    reason: reason ?? null,
  })
}
