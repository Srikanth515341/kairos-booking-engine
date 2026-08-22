import type { AvailabilityBusyBlock } from '../api/types'

/**
 * The grid renders three kinds of busy cell, merged into one list:
 * real busy blocks from the server (confirmed bookings AND held slots —
 * structurally indistinguishable, see api/types.ts), a booking this
 * session just created (rendered immediately from the 201 response,
 * before any refetch), and one optimistic `pending` block while a create
 * request is in flight. `pending` is a PURELY LOCAL UI concept — it must
 * never be confused with the backend's own `held` status, which this
 * type has no way to see at all.
 */
export interface DisplayBlock extends AvailabilityBusyBlock {
  pending?: boolean
}
