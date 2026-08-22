import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/errors'
import type { WaitlistEntry } from '../api/types'
import * as waitlistApi from '../api/waitlist'
import { JoinWaitlistPanel } from './JoinWaitlistPanel'

vi.mock('../api/waitlist')

const mockedJoinWaitlist = vi.mocked(waitlistApi.joinWaitlist)

const START = new Date('2026-09-01T10:00:00Z')
const END = new Date('2026-09-01T11:00:00Z')

const JOINED_ENTRY: WaitlistEntry = {
  id: 'entry-1',
  resource_id: 'res-1',
  start: START.toISOString(),
  end: END.toISOString(),
  status: 'waiting',
  joined_at: '2026-08-22T00:00:00Z',
  queue_position: 3,
  active_offer: null,
}

function renderPanel(onBookInstead = vi.fn(), onClose = vi.fn()) {
  render(
    <JoinWaitlistPanel
      resourceId="res-1"
      start={START}
      end={END}
      timeZone="UTC"
      onClose={onClose}
      onBookInstead={onBookInstead}
    />,
  )
  return { onBookInstead, onClose }
}

describe('JoinWaitlistPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('explains the containment eligibility rule where the user is deciding whether to join (PRD FR21)', () => {
    renderPanel()
    expect(
      screen.getByText(
        /only be offered this slot if the ENTIRE freed time covers your full request/,
      ),
    ).toBeInTheDocument()
  })

  it('shows the queue position on a successful join', async () => {
    const user = userEvent.setup()
    mockedJoinWaitlist.mockResolvedValue(JOINED_ENTRY)
    renderPanel()

    await user.click(screen.getByRole('button', { name: 'Join waitlist' }))

    expect(await screen.findByText(/position #3/)).toBeInTheDocument()
    expect(mockedJoinWaitlist).toHaveBeenCalledWith({
      resource_id: 'res-1',
      start: START.toISOString(),
      end: END.toISOString(),
    })
  })

  it('handles 422 slot_already_available gracefully — not as an error, offers to book directly instead', async () => {
    const user = userEvent.setup()
    mockedJoinWaitlist.mockRejectedValue(
      new ApiError(
        { code: 'slot_already_available', message: 'Free now.', details: {}, request_id: null },
        422,
      ),
    )
    const { onBookInstead } = renderPanel()

    await user.click(screen.getByRole('button', { name: 'Join waitlist' }))

    const notice = await screen.findByText(/available right now/)
    expect(notice).toBeInTheDocument()
    // Never rendered as a generic error — this is an advisory, expected outcome.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Book this slot' }))
    expect(onBookInstead).toHaveBeenCalledWith(START, END)
  })

  it('handles 409 already_on_waitlist gracefully, without a generic error banner', async () => {
    const user = userEvent.setup()
    mockedJoinWaitlist.mockRejectedValue(
      new ApiError(
        { code: 'already_on_waitlist', message: 'Already there.', details: {}, request_id: null },
        409,
      ),
    )
    renderPanel()

    await user.click(screen.getByRole('button', { name: 'Join waitlist' }))

    expect(await screen.findByText(/already on the waitlist for this time/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('renders a real error banner for a genuine failure', async () => {
    const user = userEvent.setup()
    mockedJoinWaitlist.mockRejectedValue(
      new ApiError(
        { code: 'internal_error', message: 'Something broke.', details: {}, request_id: null },
        500,
      ),
    )
    renderPanel()

    await user.click(screen.getByRole('button', { name: 'Join waitlist' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Something broke.')
  })
})
