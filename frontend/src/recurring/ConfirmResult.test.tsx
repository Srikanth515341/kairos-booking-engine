import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { RecurringConfirmResponse, RecurringSeriesCancelResponse } from '../api/types'
import { ConfirmResult } from './ConfirmResult'

const RESULT: RecurringConfirmResponse = {
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
    { occurrence_date: '2026-09-15', reason: 'slot_unavailable', acknowledged: false },
  ],
}

describe('ConfirmResult', () => {
  it('renders a 207 with conflicts as a SUCCESS, never a generic error state', () => {
    render(
      <ConfirmResult
        result={RESULT}
        timeZone="UTC"
        onCancelSeries={vi.fn()}
        onStartNew={vi.fn()}
      />,
    )
    // No role="alert" anywhere for the confirmation itself — this is a
    // success rendering, not an error one (Spec v1.0 §10).
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByText('Series confirmed')).toBeInTheDocument()
    expect(screen.getByText(/1 occurrence created, 2 not created/)).toBeInTheDocument()
  })

  it('gives acknowledged:false distinct wording — "taken while you were confirming", never the same text as an already-seen conflict', () => {
    render(
      <ConfirmResult
        result={RESULT}
        timeZone="UTC"
        onCancelSeries={vi.fn()}
        onStartNew={vi.fn()}
      />,
    )
    const takenWhileConfirming = screen.getByText(/this slot was taken while you were confirming/)
    const alreadySaw = screen.getByText(/skipped, as acknowledged in preview/)
    expect(takenWhileConfirming).toBeInTheDocument()
    expect(alreadySaw).toBeInTheDocument()
    expect(takenWhileConfirming.textContent).not.toBe(alreadySaw.textContent)
  })

  it('renders the acknowledged:false conflict in a visually distinct container from the acknowledged:true one', () => {
    render(
      <ConfirmResult
        result={RESULT}
        timeZone="UTC"
        onCancelSeries={vi.fn()}
        onStartNew={vi.fn()}
      />,
    )
    const unacknowledgedItem = screen
      .getByText(/this slot was taken while you were confirming/)
      .closest('li')
    const acknowledgedItem = screen.getByText(/skipped, as acknowledged in preview/).closest('li')
    expect(unacknowledgedItem).not.toBeNull()
    expect(acknowledgedItem).not.toBeNull()
    expect(unacknowledgedItem?.className).not.toBe(acknowledgedItem?.className)
  })

  it('lets the user cancel the whole series, and shows the result once cancelled', async () => {
    const user = userEvent.setup()
    const cancelResponse: RecurringSeriesCancelResponse = {
      series_id: 'series-1',
      cancelled_booking_ids: ['b2', 'b3'],
      occurrences_already_past: 1,
    }
    const onCancelSeries = vi.fn().mockResolvedValue(cancelResponse)

    render(
      <ConfirmResult
        result={RESULT}
        timeZone="UTC"
        onCancelSeries={onCancelSeries}
        onStartNew={vi.fn()}
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Cancel this series' }))

    expect(onCancelSeries).toHaveBeenCalledWith(undefined)
    expect(await screen.findByText(/2 future occurrences cancelled/)).toBeInTheDocument()
    expect(screen.getByText(/1 already-past occurrence left untouched/)).toBeInTheDocument()
  })
})
