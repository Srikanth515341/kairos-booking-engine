import { apiClient } from './client'
import { buildQuery } from './query'
import type {
  AdminChecksLatestResponse,
  BookingHistoryResponse,
  DeactivateUserResponse,
  Resource,
  ResourceAdminGrant,
  ResourceCreateInput,
  ResourceUpdateInput,
  ResourceUtilization,
} from './types'

/** `POST /resources` — `system_admin` only; no `Idempotency-Key` (Spec
 * v1.0 §7's coverage list never names this endpoint). */
export function createResource(input: ResourceCreateInput): Promise<Resource> {
  return apiClient.post<Resource>('/resources', input)
}

/** `PATCH /resources/{id}` — resource admin (of THIS resource) or
 * `system_admin`. A real partial update: only keys present in `input`
 * change. Setting `status: 'inactive'` is how a resource goes offline —
 * there is no DELETE endpoint anywhere in this API. */
export function updateResource(id: string, input: ResourceUpdateInput): Promise<Resource> {
  return apiClient.patch<Resource>(`/resources/${id}`, input)
}

export function grantResourceAdmin(
  resourceId: string,
  userId: string,
): Promise<ResourceAdminGrant> {
  return apiClient.post<ResourceAdminGrant>(`/resources/${resourceId}/admins`, {
    user_id: userId,
  })
}

/** 204 No Content on success. */
export async function revokeResourceAdmin(resourceId: string, userId: string): Promise<void> {
  await apiClient.delete(`/resources/${resourceId}/admins/${userId}`)
}

/** `GET /bookings/{id}/history` — owner, resource admin, or `operations`;
 * a 404 (never 403) hides existence from anyone else, the same object-
 * level opacity every other booking endpoint in this API already uses. */
export function getBookingHistory(bookingId: string): Promise<BookingHistoryResponse> {
  return apiClient.get<BookingHistoryResponse>(`/bookings/${bookingId}/history`)
}

/** `GET /admin/checks/latest` — `operations` or `system_admin`. */
export function getAdminChecksLatest(): Promise<AdminChecksLatestResponse> {
  return apiClient.get<AdminChecksLatestResponse>('/admin/checks/latest')
}

/** `GET /admin/resources/{id}/utilization` — resource admin (of THIS
 * resource), `operations`, or `system_admin`. `from`/`to` accept a bare
 * date or a full ISO instant. */
export function getResourceUtilization(
  resourceId: string,
  from: string,
  to: string,
): Promise<ResourceUtilization> {
  return apiClient.get<ResourceUtilization>(
    `/admin/resources/${resourceId}/utilization${buildQuery({ from, to })}`,
  )
}

/** `POST /admin/users/{id}/deactivate` — `system_admin` only,
 * `Idempotency-Key` required. Repeat deactivation is an idempotent
 * no-op: 200 with zeroed cascade counts, `status` still `"deactivated"`. */
export function deactivateUser(userId: string, reason: string): Promise<DeactivateUserResponse> {
  return apiClient.post<DeactivateUserResponse>(`/admin/users/${userId}/deactivate`, { reason })
}
