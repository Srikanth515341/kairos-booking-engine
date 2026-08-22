import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as adminApi from '../api/admin'
import { ApiError } from '../api/errors'
import { BookingHistoryPage } from './BookingHistoryPage'

vi.mock('../api/admin')

const mockedGetBookingHistory = vi.mocked(adminApi.getBookingHistory)

function renderAt(bookingId: string) {
  render(
    <MemoryRouter initialEntries={[`/bookings/${bookingId}/history`]}>
      <Routes>
        <Route path="/bookings/:id/history" element={<BookingHistoryPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('BookingHistoryPage', () => {
  it('reconstructs a full lifecycle with actors, reasons, and field-level changes', async () => {
    mockedGetBookingHistory.mockResolvedValue({
      booking_id: 'b1',
      events: [
        {
          occurred_at: '2026-08-19T10:15:00Z',
          action: 'insert',
          actor: { id: 'u1', display_name: 'Alex Chen', type: 'user' },
          reason: null,
          request_id: 'req-1',
          changes: { status: [null, 'confirmed'] },
        },
        {
          occurred_at: '2026-08-20T08:02:00Z',
          action: 'update',
          actor: { id: 'admin1', display_name: 'Facilities Bot', type: 'admin' },
          reason: 'Room offline for maintenance',
          request_id: 'req-2',
          changes: { status: ['confirmed', 'cancelled'] },
        },
      ],
    })

    renderAt('b1')

    expect(await screen.findByText('Alex Chen', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('Facilities Bot', { exact: false })).toBeInTheDocument()
    expect(screen.getByText(/Room offline for maintenance/)).toBeInTheDocument()
    expect(screen.getByText(/confirmed → cancelled/)).toBeInTheDocument()
    expect(screen.getByText('Admin override:', { exact: false })).toBeInTheDocument()
  })

  it('renders a 404 as "doesn\'t exist or you don\'t have permission" — the same opacity every other booking endpoint uses — never a generic error', async () => {
    mockedGetBookingHistory.mockRejectedValue(
      new ApiError({ code: 'not_found', message: 'nope', details: {}, request_id: null }, 404),
    )
    renderAt('b2')
    expect(
      await screen.findByText(/does not exist, or you do not have permission to view it/),
    ).toBeInTheDocument()
  })
})
