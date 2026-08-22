import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/errors'
import * as resourcesApi from '../api/resources'
import type { Resource, WaitlistEntry } from '../api/types'
import * as waitlistApi from '../api/waitlist'
import { MyWaitlistPage } from './MyWaitlistPage'

vi.mock('../api/resources')
vi.mock('../api/waitlist')

const mockedListResources = vi.mocked(resourcesApi.listResources)
const mockedListWaitlistEntries = vi.mocked(waitlistApi.listWaitlistEntries)
const mockedConfirmOffer = vi.mocked(waitlistApi.confirmWaitlistOffer)
const mockedDeclineOffer = vi.mocked(waitlistApi.declineWaitlistOffer)

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

const WAITING_ENTRY: WaitlistEntry = {
  id: 'entry-waiting',
  resource_id: 'res-1',
  start: '2026-09-01T10:00:00Z',
  end: '2026-09-01T11:00:00Z',
  status: 'waiting',
  joined_at: '2026-08-20T00:00:00Z',
  queue_position: 2,
  active_offer: null,
}

const OFFERED_ENTRY: WaitlistEntry = {
  id: 'entry-offered',
  resource_id: 'res-1',
  start: '2026-09-02T10:00:00Z',
  end: '2026-09-02T11:00:00Z',
  status: 'offered',
  joined_at: '2026-08-19T00:00:00Z',
  queue_position: null,
  active_offer: { id: 'offer-1', expires_at: '2026-12-01T00:15:00Z' },
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedListResources.mockResolvedValue({ data: [RESOURCE], next_cursor: null })
  mockedListWaitlistEntries.mockResolvedValue({
    data: [WAITING_ENTRY, OFFERED_ENTRY],
    next_cursor: null,
  })
})

describe('MyWaitlistPage', () => {
  it('displays the queue position for a waiting entry', async () => {
    render(<MyWaitlistPage />)
    expect(await screen.findByText('Position #2')).toBeInTheDocument()
  })

  it('shows a countdown plus Confirm/Decline for an offered entry', async () => {
    render(<MyWaitlistPage />)
    await screen.findByText(/remaining/)
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Decline' })).toBeInTheDocument()
  })

  it('explains the containment eligibility rule on the page itself (PRD FR21)', async () => {
    render(<MyWaitlistPage />)
    await screen.findByText('Position #2')
    expect(
      screen.getByText(
        /offered a freed slot only when the ENTIRE freed time covers your full request/,
      ),
    ).toBeInTheDocument()
  })

  it('renders 409 offer_expired as an expected outcome (role="status"), never a generic error, and refreshes the entry', async () => {
    const user = userEvent.setup()
    mockedConfirmOffer.mockRejectedValue(
      new ApiError(
        { code: 'offer_expired', message: 'Expired.', details: {}, request_id: null },
        409,
      ),
    )
    render(<MyWaitlistPage />)
    await screen.findByRole('button', { name: 'Confirm' })

    await user.click(screen.getByRole('button', { name: 'Confirm' }))

    const statusMessage = await screen.findByText(/This offer has expired/)
    expect(statusMessage).toHaveAttribute('role', 'status')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    // The list is refetched so the entry's current state is shown, not left stale.
    await waitFor(() => {
      expect(mockedListWaitlistEntries).toHaveBeenCalledTimes(2)
    })
  })

  it('declines an offer and shows the cascade-to-next-person outcome', async () => {
    const user = userEvent.setup()
    mockedDeclineOffer.mockResolvedValue({
      id: 'offer-1',
      waitlist_entry_id: 'entry-offered',
      hold_booking_id: 'hold-1',
      resource_id: 'res-1',
      start: OFFERED_ENTRY.start,
      end: OFFERED_ENTRY.end,
      status: 'declined',
      expires_at: OFFERED_ENTRY.active_offer?.expires_at ?? '',
      created_at: '2026-08-19T00:00:00Z',
    })
    render(<MyWaitlistPage />)
    await screen.findByRole('button', { name: 'Decline' })

    await user.click(screen.getByRole('button', { name: 'Decline' }))

    expect(await screen.findByText(/offered to the next person in line/)).toBeInTheDocument()
    expect(mockedDeclineOffer).toHaveBeenCalledWith('offer-1')
  })
})
