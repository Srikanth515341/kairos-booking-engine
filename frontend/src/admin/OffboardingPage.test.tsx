import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as adminApi from '../api/admin'
import { ApiError } from '../api/errors'
import { OffboardingPage } from './OffboardingPage'

vi.mock('../api/admin')

const mockedDeactivateUser = vi.mocked(adminApi.deactivateUser)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('OffboardingPage', () => {
  it('submits the user id and reason, and renders the per-policy cascade summary', async () => {
    const user = userEvent.setup()
    mockedDeactivateUser.mockResolvedValue({
      user_id: 'u1',
      status: 'deactivated',
      bookings_transferred: 4,
      bookings_cancelled: 0,
      bookings_retained: 2,
      waitlist_entries_cancelled: 3,
      offers_released: 1,
      series_flagged_for_admin: [
        { series_id: 's1', resource_id: 'res-1', occurrences_remaining: 6 },
      ],
    })

    render(<OffboardingPage />)
    await user.type(screen.getByLabelText('User ID'), 'u1')
    await user.type(screen.getByLabelText('Reason'), 'Employee offboarding — ticket HR-4821')
    await user.click(screen.getByRole('button', { name: 'Deactivate user' }))

    expect(mockedDeactivateUser).toHaveBeenCalledWith('u1', 'Employee offboarding — ticket HR-4821')
    expect(await screen.findByText(/u1 is now deactivated/)).toBeInTheDocument()
    expect(screen.getByText('4')).toBeInTheDocument() // transferred
    expect(screen.getByText('2')).toBeInTheDocument() // retained
    expect(screen.getByText('3')).toBeInTheDocument() // waitlist cancelled
    expect(screen.getByText(/needs a resource admin.s decision/)).toBeInTheDocument()
  })

  it('shows a specific permission-denied message on 403, not a generic error', async () => {
    const user = userEvent.setup()
    mockedDeactivateUser.mockRejectedValue(
      new ApiError(
        { code: 'permission_denied', message: 'nope', details: {}, request_id: null },
        403,
      ),
    )

    render(<OffboardingPage />)
    await user.type(screen.getByLabelText('User ID'), 'u1')
    await user.type(screen.getByLabelText('Reason'), 'test')
    await user.click(screen.getByRole('button', { name: 'Deactivate user' }))

    expect(
      await screen.findByText(/need system administrator access to deactivate a user/),
    ).toBeInTheDocument()
  })
})
