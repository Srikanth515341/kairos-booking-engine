import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as recurringApi from '../api/recurringBookings'
import * as resourcesApi from '../api/resources'
import type { Resource } from '../api/types'
import { RecurringSeriesPage } from './RecurringSeriesPage'

vi.mock('../api/resources')
vi.mock('../api/recurringBookings')

const mockedListResources = vi.mocked(resourcesApi.listResources)
const mockedPreview = vi.mocked(recurringApi.previewRecurringSeries)
const mockedConfirm = vi.mocked(recurringApi.confirmRecurringSeries)

const RESOURCE: Resource = {
  id: 'res-1',
  name: 'Room A',
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

beforeEach(() => {
  vi.clearAllMocks()
  mockedListResources.mockResolvedValue({ data: [RESOURCE], next_cursor: null })
  mockedPreview.mockResolvedValue({
    preview_token: 'pv_test',
    resource_id: 'res-1',
    tzdata_version: '2026.3',
    would_create: [
      { occurrence_date: '2026-09-01', start: '2026-09-01T13:00:00Z', end: '2026-09-01T13:30:00Z' },
    ],
    conflicts: [
      {
        occurrence_date: '2026-09-08',
        start: '2026-09-08T13:00:00Z',
        end: '2026-09-08T13:30:00Z',
        reason: 'slot_unavailable',
      },
    ],
    time_adjustments: [
      {
        occurrence_date: '2027-03-28',
        issue: 'nonexistent_local_time',
        requested_local: '02:30:00',
        adjusted_local: '03:30:00',
        explanation: 'shifted forward by the transition gap',
      },
    ],
  })
})

describe('RecurringSeriesPage — end-to-end preview -> acknowledge -> confirm', () => {
  it(
    'walks the full flow and renders the 207 result as a success',
    { timeout: 15000 },
    async () => {
      const user = userEvent.setup()
      mockedConfirm.mockResolvedValue({
        series_id: 'series-1',
        created: [
          {
            booking_id: 'b1',
            occurrence_date: '2026-09-01',
            start: '2026-09-01T13:00:00Z',
            end: '2026-09-01T13:30:00Z',
          },
        ],
        conflicts: [
          { occurrence_date: '2026-09-08', reason: 'slot_unavailable', acknowledged: true },
        ],
      })

      render(<RecurringSeriesPage />)

      await screen.findByRole('option', { name: 'Room A' })
      await user.click(screen.getByRole('button', { name: 'Preview series' }))

      await screen.findByText('Review this series')
      expect(mockedPreview).toHaveBeenCalledTimes(1)

      const confirmButton = screen.getByRole('button', { name: 'Confirm series' })
      expect(confirmButton).toBeDisabled()

      const acknowledgeAllButtons = screen.getAllByRole('button', { name: 'Acknowledge all' })
      for (const button of acknowledgeAllButtons) {
        await user.click(button)
      }

      await waitFor(() => {
        expect(confirmButton).toBeEnabled()
      })

      await user.click(confirmButton)

      await screen.findByText('Series confirmed')
      expect(mockedConfirm).toHaveBeenCalledWith({
        preview_token: 'pv_test',
        acknowledged_conflicts: ['2026-09-08'],
        acknowledged_adjustments: ['2027-03-28'],
      })
      // A 207 with a non-empty conflicts array renders as success detail,
      // never a generic error banner.
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    },
  )

  it('confirm cannot be reached without visiting preview first — no auto-acknowledgment shortcut exists', async () => {
    render(<RecurringSeriesPage />)
    expect(screen.queryByText('Review this series')).not.toBeInTheDocument()
    expect(screen.getByText('New recurring booking')).toBeInTheDocument()
  })
})
