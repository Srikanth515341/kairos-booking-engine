/**
 * Response shapes confirmed against the real backend source (serializers,
 * views, and their own tests), not transcribed from the API spec doc alone
 * — see kairos/resources/serializers.py, kairos/bookings/serializers.py,
 * kairos/core/pagination.py.
 */

export type ResourceStatus = 'active' | 'inactive'
export type ResourceOffboardingPolicy = 'transfer' | 'cancel_and_notify' | 'retain'

export interface Resource {
  id: string
  name: string
  category: string
  timezone: string
  bookable_start_time: string
  bookable_end_time: string
  max_booking_duration_minutes: number | null
  offboarding_policy: ResourceOffboardingPolicy
  status: ResourceStatus
  created_by: string
  created_at: string
}

/**
 * `booking_id`/`owner` are KEY-ABSENT (not null) unless the requester owns
 * the booking or administers the resource — and, unconditionally, a HELD
 * booking never reveals either field to ANYONE, including a resource
 * admin (kairos/resources/views.py: `reveal = status != HELD and (...)`).
 * A held slot is therefore structurally indistinguishable from another
 * user's confirmed booking at this type's own level — there is no field
 * to check, which is what makes it impossible for any code that reads
 * this type to accidentally leak hold state. See docs/03-api-data-spec.md
 * §5.7/§10: "Do not attempt to distinguish or label it."
 */
export interface AvailabilityBusyBlock {
  start: string
  end: string
  booking_id?: string
  owner?: { id: string; display_name: string }
}

export interface AvailabilityResponse {
  resource_id: string
  timezone: string
  range: { from: string; to: string }
  as_of: string
  data_freshness: string
  busy_blocks: AvailabilityBusyBlock[]
}

export type BookingStatus = 'confirmed' | 'held' | 'cancelled'

export interface Booking {
  id: string
  resource_id: string
  user_id: string
  start: string
  end: string
  status: BookingStatus
  series_id: string | null
  created_at: string
  cancelled_at: string | null
  cancelled_by: string | null
  cancellation_reason: string | null
}

export interface Paginated<T> {
  data: T[]
  next_cursor: string | null
}
