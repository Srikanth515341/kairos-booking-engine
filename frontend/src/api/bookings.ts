import { apiClient } from './client'
import { buildQuery } from './query'
import type { Booking, Paginated } from './types'

export interface ListBookingsParams {
  /** Omitted → the caller's OWN bookings only (kairos/bookings/views.py:
   * `resource_id` requires resource-admin/operations scope, so an
   * ordinary booker never passes it). */
  resourceId?: string
  status?: 'confirmed' | 'cancelled'
  time?: 'upcoming' | 'past' | 'all'
  cursor?: string
  limit?: number
}

export function listBookings(params: ListBookingsParams = {}): Promise<Paginated<Booking>> {
  return apiClient.get<Paginated<Booking>>(
    `/bookings${buildQuery({
      resource_id: params.resourceId,
      status: params.status,
      time: params.time,
      cursor: params.cursor,
      limit: params.limit,
    })}`,
  )
}

export function getBooking(id: string): Promise<Booking> {
  return apiClient.get<Booking>(`/bookings/${id}`)
}

export interface CreateBookingInput {
  resource_id: string
  start: string
  end: string
}

export function createBooking(input: CreateBookingInput): Promise<Booking> {
  return apiClient.post<Booking>('/bookings', input)
}

export interface EditBookingInput {
  start: string
  end: string
}

export function editBooking(id: string, input: EditBookingInput): Promise<Booking> {
  return apiClient.patch<Booking>(`/bookings/${id}`, input)
}

/**
 * `reason` is required by the backend only when the caller is not the
 * booking's owner (an admin override) — an ordinary self-cancel may omit
 * it. This module doesn't enforce that distinction; the backend's own
 * `validation_error` response is the source of truth, surfaced to the
 * caller via the thrown ApiError.
 */
export function cancelBooking(id: string, reason?: string): Promise<Booking> {
  return apiClient.post<Booking>(`/bookings/${id}/cancel`, { reason: reason ?? null })
}
