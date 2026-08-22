import { apiClient } from './client'
import { buildQuery } from './query'
import type {
  Paginated,
  WaitlistEntry,
  WaitlistEntryStatus,
  WaitlistOfferConfirmResponse,
  WaitlistOfferDeclineResponse,
} from './types'

export interface JoinWaitlistInput {
  resource_id: string
  start: string
  end: string
}

/** `POST /waitlist-entries` — no availability pre-check server-side (the
 * SAME "the constraint/index IS the check" philosophy booking creation
 * uses); a 422 `slot_already_available` means the range is free right
 * now and should be booked directly instead — see api/errors.ts. */
export function joinWaitlist(input: JoinWaitlistInput): Promise<WaitlistEntry> {
  return apiClient.post<WaitlistEntry>('/waitlist-entries', input)
}

export interface ListWaitlistEntriesParams {
  status?: WaitlistEntryStatus
  cursor?: string
  limit?: number
}

/** `GET /waitlist-entries` — always self-scoped server-side (no `user_id`
 * param exists to pass). Returns every status unless `status` is given —
 * NOT limited to `waiting`/`offered` by default. */
export function listWaitlistEntries(
  params: ListWaitlistEntriesParams = {},
): Promise<Paginated<WaitlistEntry>> {
  return apiClient.get<Paginated<WaitlistEntry>>(
    `/waitlist-entries${buildQuery({
      status: params.status,
      cursor: params.cursor,
      limit: params.limit,
    })}`,
  )
}

export function cancelWaitlistEntry(id: string): Promise<WaitlistEntry> {
  return apiClient.post<WaitlistEntry>(`/waitlist-entries/${id}/cancel`)
}

/** `POST /waitlist-offers/{id}/confirm` — the literal conditional
 * acceptance UPDATE; 409 `offer_expired` is a real, EXPECTED outcome any
 * time the offer's window has passed by the moment this request lands
 * server-side, not a bug — the client's own countdown is advisory only
 * (Spec v1.0 §5.13, Phase 26 Scope IN). Callers must not treat this
 * request as blocked just because a local countdown has visually reached
 * zero. */
export function confirmWaitlistOffer(offerId: string): Promise<WaitlistOfferConfirmResponse> {
  return apiClient.post<WaitlistOfferConfirmResponse>(`/waitlist-offers/${offerId}/confirm`)
}

/** `POST /waitlist-offers/{id}/decline` — releases the hold immediately
 * and cascades to the next eligible entry sooner than expiry would. */
export function declineWaitlistOffer(offerId: string): Promise<WaitlistOfferDeclineResponse> {
  return apiClient.post<WaitlistOfferDeclineResponse>(`/waitlist-offers/${offerId}/decline`)
}
