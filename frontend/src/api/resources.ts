import { apiClient } from './client'
import { buildQuery } from './query'
import type { AvailabilityResponse, Paginated, Resource } from './types'

export function listResources(params?: {
  cursor?: string
  limit?: number
}): Promise<Paginated<Resource>> {
  return apiClient.get<Paginated<Resource>>(
    `/resources${buildQuery({ cursor: params?.cursor, limit: params?.limit })}`,
  )
}

export function getResource(id: string): Promise<Resource> {
  return apiClient.get<Resource>(`/resources/${id}`)
}

/**
 * `from`/`to` must be ISO instants (UTC) — the caller is responsible for
 * converting the viewer's local calendar range into UTC before calling
 * this (see booking/dateGrid.ts). Bounded to 92 days server-side
 * (kairos/resources/views.py: MAX_AVAILABILITY_QUERY_DAYS).
 */
export function getAvailability(
  resourceId: string,
  from: string,
  to: string,
): Promise<AvailabilityResponse> {
  return apiClient.get<AvailabilityResponse>(
    `/resources/${resourceId}/availability${buildQuery({ from, to })}`,
  )
}
