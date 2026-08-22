import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as bookingsApi from '../api/bookings'
import { ApiError } from '../api/errors'
import * as resourcesApi from '../api/resources'
import type { Booking, Resource } from '../api/types'
import { CalendarPage } from './CalendarPage'

vi.mock('../api/resources')
vi.mock('../api/bookings')

const mockedListResources = vi.mocked(resourcesApi.listResources)
const mockedGetAvailability = vi.mocked(resourcesApi.getAvailability)
const mockedCreateBooking = vi.mocked(bookingsApi.createBooking)

const RESOURCE: Resource = {
  id: 'res-1',
  name: 'Conference Room A',
  category: 'room',
  timezone: 'UTC',
  bookable_start_time: '00:00:00',
  bookable_end_time: '23:59:59',
  max_booking_duration_minutes: null,
  offboarding_policy: 'retain',
  status: 'active',
  created_by: 'u1',
  created_at: '2026-01-01T00:00:00Z',
}

const CREATED_BOOKING: Booking = {
  id: 'b1',
  resource_id: 'res-1',
  user_id: 'u1',
  start: '2026-09-01T09:00:00Z',
  end: '2026-09-01T10:00:00Z',
  status: 'confirmed',
  series_id: null,
  created_at: '2026-09-01T09:00:00Z',
  cancelled_at: null,
  cancelled_by: null,
  cancellation_reason: null,
}

async function openNewBookingPanel(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() => {
    expect(mockedGetAvailability).toHaveBeenCalled()
  })
  const bookButtons = await screen.findAllByRole('button', { name: /^Book / })
  const firstBookButton = bookButtons[0]
  expect(firstBookButton).toBeDefined()
  if (firstBookButton) {
    await user.click(firstBookButton)
  }
  await screen.findByText('New booking')
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedListResources.mockResolvedValue({ data: [RESOURCE], next_cursor: null })
  mockedGetAvailability.mockResolvedValue({
    resource_id: 'res-1',
    timezone: 'UTC',
    range: { from: '2026-08-31T00:00:00Z', to: '2026-09-07T00:00:00Z' },
    as_of: '2026-09-01T00:00:00Z',
    data_freshness: 'primary',
    busy_blocks: [],
  })
})

describe('CalendarPage — booking creation optimistic UI', () => {
  it(
    'renders the new booking as pending immediately, then finalizes on 201',
    { timeout: 15000 },
    async () => {
      const user = userEvent.setup()
      let resolveCreate!: (booking: Booking) => void
      mockedCreateBooking.mockReturnValue(
        new Promise<Booking>((resolve) => {
          resolveCreate = resolve
        }),
      )

      render(<CalendarPage />)
      await openNewBookingPanel(user)
      await user.click(screen.getByRole('button', { name: 'Book' }))

      // Optimistic: a pending cell renders before the request has resolved.
      await waitFor(() => {
        expect(document.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0)
      })
      expect(screen.getByText('New booking')).toBeInTheDocument()

      resolveCreate(CREATED_BOOKING)

      // Finalized: the panel closes and the pending styling is gone.
      await waitFor(() => {
        expect(screen.queryByText('New booking')).not.toBeInTheDocument()
      })
      expect(document.querySelectorAll('.animate-pulse').length).toBe(0)
      expect(mockedCreateBooking).toHaveBeenCalledWith(
        expect.objectContaining({ resource_id: 'res-1' }),
      )
    },
  )

  it(
    'rolls back the optimistic state, shows a specific conflict message, and re-fetches availability on 409 slot_unavailable',
    { timeout: 15000 },
    async () => {
      const user = userEvent.setup()
      mockedCreateBooking.mockRejectedValue(
        new ApiError(
          { code: 'slot_unavailable', message: 'Slot unavailable.', details: {}, request_id: 'r1' },
          409,
        ),
      )

      render(<CalendarPage />)
      await openNewBookingPanel(user)
      expect(mockedGetAvailability).toHaveBeenCalledTimes(1)

      await user.click(screen.getByRole('button', { name: 'Book' }))

      await waitFor(() => {
        expect(screen.getByRole('alert')).toHaveTextContent(/Someone booked this a moment ago/)
      })

      // Never the generic, unhelpful phrasing PRD R6 flags as a real risk.
      expect(screen.getByRole('alert').textContent?.toLowerCase()).not.toContain('booking failed')

      // Rolled back: no pending (optimistic) cell survives the rejection.
      expect(document.querySelectorAll('.animate-pulse').length).toBe(0)

      // Stale view refreshed, per Phase 24's own wording, rather than left showing what led to the conflict.
      await waitFor(() => {
        expect(mockedGetAvailability).toHaveBeenCalledTimes(2)
      })
    },
  )
})
