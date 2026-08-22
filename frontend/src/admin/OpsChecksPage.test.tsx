import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as adminApi from '../api/admin'
import { ApiError } from '../api/errors'
import type { AdminCheck } from '../api/types'
import { OpsChecksPage } from './OpsChecksPage'

vi.mock('../api/admin')

const mockedGetChecks = vi.mocked(adminApi.getAdminChecksLatest)

function check(overrides: Partial<AdminCheck> & Pick<AdminCheck, 'check_name'>): AdminCheck {
  return { last_run_at: '2026-08-22T10:00:00Z', status: 'pass', findings: {}, ...overrides }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('OpsChecksPage', () => {
  it('renders all six checks in the documented order, even when the backend omits one', async () => {
    mockedGetChecks.mockResolvedValue({
      checks: [
        check({ check_name: 'reconciliation' }),
        check({ check_name: 'schema_assertion' }),
        // hold_reaper deliberately omitted — a check that never ran might
        // not even appear; the page must still show it, honestly, as
        // never-run rather than silently dropping the row.
        check({ check_name: 'offer_cascade' }),
        check({ check_name: 'series_materialization' }),
        check({ check_name: 'tzdata_rematerialization' }),
      ],
    })

    render(<OpsChecksPage />)

    const headings = await screen.findAllByRole('heading', { level: 2 })
    expect(headings.map((h) => h.textContent)).toEqual([
      'Schema assertion',
      'Reconciliation',
      'Hold reaper',
      'Offer cascade',
      'Series materialization',
      'Timezone data re-materialization',
    ])
  })

  it('shows a genuinely never-run check as "Never run", never a faked pass', async () => {
    mockedGetChecks.mockResolvedValue({
      checks: [
        check({ check_name: 'offer_cascade', last_run_at: null, status: null, findings: {} }),
      ],
    })
    render(<OpsChecksPage />)
    await screen.findByText('Offer cascade')
    expect(screen.getAllByText('Never run').length).toBeGreaterThan(0)
  })

  it('always shows the reconciliation explanation, even when it is passing', async () => {
    mockedGetChecks.mockResolvedValue({
      checks: [check({ check_name: 'reconciliation', findings: { overlaps_found: 0 } })],
    })
    render(<OpsChecksPage />)
    expect(await screen.findByText(/This is NOT a race detector/)).toBeInTheDocument()
  })

  it('on a real reconciliation failure, renders the exact backend message plus the "guarantee removed, not a race" framing, as an alert', async () => {
    const backendMessage =
      'This is NOT a race detector. If the no_overlapping_bookings constraint is present and functioning, this query is structurally incapable of returning a row. A hit means the correctness guarantee has been REMOVED — a dropped constraint, a restore taken without it, or an out-of-band write that bypassed it — NOT that a race occurred. Treating this as a timing issue and waiting for it to resolve is the wrong response.'
    mockedGetChecks.mockResolvedValue({
      checks: [
        check({
          check_name: 'reconciliation',
          status: 'fail',
          findings: {
            overlaps_found: 1,
            flagged_booking_ids: ['b1', 'b2'],
            message: backendMessage,
          },
        }),
      ],
    })
    render(<OpsChecksPage />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(backendMessage)
    expect(alert).toHaveTextContent(/REMOVED/)
    expect(screen.getByText('Fail')).toBeInTheDocument()
  })

  it('renders a specific permission-denied message on 403, not a generic error', async () => {
    mockedGetChecks.mockRejectedValue(
      new ApiError(
        { code: 'permission_denied', message: 'no', details: {}, request_id: null },
        403,
      ),
    )
    render(<OpsChecksPage />)
    expect(
      await screen.findByText(/need operations or system administrator access/),
    ).toBeInTheDocument()
  })
})
