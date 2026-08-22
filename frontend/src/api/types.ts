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

/**
 * Recurring-series preview/confirm shapes — confirmed against the real
 * backend source (kairos/bookings/recurring_series.py, serializers.py,
 * views.py), not the API spec doc alone. Preview's `conflicts[i]` and
 * confirm's `conflicts[i]` are GENUINELY DIFFERENT shapes (preview's
 * carries `start`/`end`, confirm's does not, but carries `acknowledged`
 * instead) — kept as two distinct interfaces rather than one shared
 * `Conflict` type, so a caller can't accidentally read a field that only
 * exists on the other one.
 */
export interface RecurringPreviewRequest {
  resource_id: string
  timezone: string
  /** "HH:MM:SS" */
  local_start_time: string
  /** "HH:MM:SS" */
  local_end_time: string
  /** 0 = Sunday … 6 = Saturday — the SAME convention `Date.getDay()` uses. */
  weekday: number
  /** "YYYY-MM-DD" */
  series_start_date: string
  occurrence_count: number
}

export interface WouldCreateItem {
  occurrence_date: string
  start: string
  end: string
}

export interface PreviewConflictItem {
  occurrence_date: string
  start: string
  end: string
  reason: string
}

export type TimeAdjustmentIssue = 'nonexistent_local_time' | 'ambiguous_local_time'

export interface TimeAdjustmentItem {
  occurrence_date: string
  issue: TimeAdjustmentIssue
  /** "HH:MM:SS" local wall-clock time, in the series' own timezone. */
  requested_local: string
  /** "HH:MM:SS" local wall-clock time, in the series' own timezone. */
  adjusted_local: string
  explanation: string
}

export interface RecurringPreviewResponse {
  preview_token: string
  resource_id: string
  tzdata_version: string
  would_create: WouldCreateItem[]
  conflicts: PreviewConflictItem[]
  time_adjustments: TimeAdjustmentItem[]
}

export interface RecurringConfirmRequest {
  preview_token: string
  acknowledged_conflicts: string[]
  acknowledged_adjustments: string[]
}

export interface CreatedOccurrenceItem {
  booking_id: string
  occurrence_date: string
  start: string
  end: string
}

export interface ConfirmConflictItem {
  occurrence_date: string
  reason: string
  /** `false` means this occurrence conflicted BETWEEN preview and confirm
   * — a genuinely different situation from a conflict the user already
   * saw and acknowledged in preview, and must be rendered distinctly
   * (Spec v1.0 §10, Phase 25 Scope IN). */
  acknowledged: boolean
}

export interface RecurringConfirmResponse {
  series_id: string
  created: CreatedOccurrenceItem[]
  conflicts: ConfirmConflictItem[]
}

export interface RecurringSeriesCancelResponse {
  series_id: string
  cancelled_booking_ids: string[]
  occurrences_already_past: number
}

/**
 * Waitlist/offer shapes — confirmed against the real backend source
 * (kairos/waitlist/serializers.py, views.py, models.py), not the API spec
 * doc alone.
 */
export type WaitlistEntryStatus = 'waiting' | 'offered' | 'fulfilled' | 'expired' | 'cancelled'
export type WaitlistOfferStatus = 'active' | 'confirmed' | 'expired' | 'declined'

/** Present only while the entry's status is `"offered"` — `null`
 * otherwise. Only these two keys; the offer's own resource/hold-booking
 * detail lives on the standalone offer response, not here. */
export interface ActiveOffer {
  id: string
  expires_at: string
}

export interface WaitlistEntry {
  id: string
  resource_id: string
  start: string
  end: string
  status: WaitlistEntryStatus
  joined_at: string
  /** `null` for any non-`"waiting"` entry — the backend only computes a
   * position within the FCFS `waiting` queue. */
  queue_position: number | null
  active_offer: ActiveOffer | null
}

/** `POST /waitlist-offers/{id}/confirm`'s 201 body — the BOOKING shape
 * (`kairos.bookings.serializers.BookingResponseSerializer`) with exactly
 * one extra key bolted on at the view layer, since `booking` itself has
 * no `waitlist_offer_id` column (Spec v1.0 §3's DDL doesn't define one). */
export interface WaitlistOfferConfirmResponse extends Booking {
  waitlist_offer_id: string
}

/** `POST /waitlist-offers/{id}/decline`'s 200 body — a genuinely
 * different, standalone offer shape from both `ActiveOffer` (above) and
 * confirm's booking-shaped response. */
export interface WaitlistOfferDeclineResponse {
  id: string
  waitlist_entry_id: string
  hold_booking_id: string
  resource_id: string
  start: string
  end: string
  status: WaitlistOfferStatus
  expires_at: string
  created_at: string
}

/**
 * Admin/operations shapes — confirmed against the real backend source
 * (kairos/resources/{serializers,views}.py, kairos/bookings/views.py,
 * kairos/core/views.py, kairos/identity/{views,services}.py), not the
 * API spec doc alone. NOTE (load-bearing for how the whole admin/
 * surfaces are built): neither the session token JWT payload nor
 * `POST /auth/token`'s response carries any role/permission claim, and
 * no "who am I" endpoint exists anywhere in this backend — confirmed by
 * reading `kairos.identity.oidc.issue_session_token` (payload is only
 * `sub`/`iat`/`exp`) and every route in `kairos/identity/urls.py`. There
 * is structurally no way for this frontend to know a priori whether the
 * current user is `system_admin`/`operations`, or a resource admin of
 * any given resource — see booking/CalendarPage-style precedent applied
 * here: every admin page shows its controls to any authenticated user
 * and lets the server's own 403 `permission_denied` be the actual
 * source of truth, rendered as a specific, expected outcome rather than
 * hidden or treated as a generic error.
 */
export interface ResourceCreateInput {
  name: string
  category?: string
  timezone: string
  bookable_start_time: string
  bookable_end_time: string
  max_booking_duration_minutes?: number | null
  offboarding_policy?: ResourceOffboardingPolicy
  restricted_group_id?: string | null
}

/** Every field optional — a real partial update, only present keys
 * change. `restricted_group_id` is deliberately absent: Spec v1.0
 * §5.14's own updatable-field list never names it (create-only). */
export interface ResourceUpdateInput {
  name?: string
  bookable_start_time?: string
  bookable_end_time?: string
  max_booking_duration_minutes?: number | null
  offboarding_policy?: ResourceOffboardingPolicy
  status?: ResourceStatus
}

export interface ResourceAdminGrant {
  resource_id: string
  user_id: string
  granted_at: string
  granted_by: string
}

export type BookingHistoryActorType = 'user' | 'admin' | 'system' | 'unknown'

export interface BookingHistoryActor {
  id: string | null
  display_name: string | null
  type: BookingHistoryActorType
}

/** Each changed field maps to a `[before, after]` pair — a genuine
 * before/after diff over every changed field, not just `status`
 * (kairos.bookings.views._compute_changes diffs the full audit-trigger
 * snapshot). */
export interface BookingHistoryEvent {
  occurred_at: string
  action: 'insert' | 'update'
  actor: BookingHistoryActor
  reason: string | null
  request_id: string
  changes: Record<string, [unknown, unknown]>
}

export interface BookingHistoryResponse {
  booking_id: string
  events: BookingHistoryEvent[]
}

/** Exact order the backend itself reports these in — reproduced here so
 * the dashboard's own row order matches Spec v1.0 §5.15's example
 * verbatim, rather than whatever order the array happens to arrive in. */
export const ADMIN_CHECK_NAMES = [
  'schema_assertion',
  'reconciliation',
  'hold_reaper',
  'offer_cascade',
  'series_materialization',
  'tzdata_rematerialization',
] as const
export type CheckName = (typeof ADMIN_CHECK_NAMES)[number]

export type CheckStatus = 'pass' | 'fail' | null

/** `message` is populated ONLY on a genuine reconciliation FAILURE
 * (kairos/core/reconciliation.py) — carries the exact RUNBOOK-01 framing
 * text explaining this is not a race detector. */
export interface ReconciliationFindings {
  overlaps_found?: number
  flagged_booking_ids?: string[]
  message?: string
}

export interface AdminCheck {
  check_name: CheckName
  /** `null` — never a faked pass — for a check that has genuinely never
   * run. */
  last_run_at: string | null
  status: CheckStatus
  findings: Record<string, unknown>
}

export interface AdminChecksLatestResponse {
  checks: AdminCheck[]
}

export interface ResourceUtilization {
  resource_id: string
  range: { from: string; to: string }
  total_bookings: number
  total_booked_minutes: number
  waitlist_joins: number
  cancellation_count: number
  offers_confirmed: number
  offers_expired: number
}

export interface FlaggedSeries {
  series_id: string
  resource_id: string
  occurrences_remaining: number
}

export interface DeactivateUserResponse {
  user_id: string
  status: 'deactivated'
  bookings_transferred: number
  bookings_cancelled: number
  bookings_retained: number
  waitlist_entries_cancelled: number
  offers_released: number
  series_flagged_for_admin: FlaggedSeries[]
}
