import { useCallback, useEffect, useState } from 'react'
import { cancelBooking, editBooking, listBookings, type ListBookingsParams } from '../api/bookings'
import { ApiError } from '../api/errors'
import type { Booking } from '../api/types'
import { fromDatetimeLocalValue, toDatetimeLocalValue } from '../booking/dateGrid'
import { useResources } from '../booking/useResources'
import { formatDateTime, getViewerTimeZone } from '../booking/timezone'

type StatusFilter = 'all' | 'confirmed' | 'cancelled'
type TimeFilter = 'upcoming' | 'past' | 'all'

export function MyBookingsPage() {
  const { resources } = useResources()
  const resourceNameById = new Map(resources.map((resource) => [resource.id, resource.name]))
  const timeZone = getViewerTimeZone()

  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [timeFilter, setTimeFilter] = useState<TimeFilter>('upcoming')
  const [cursorStack, setCursorStack] = useState<(string | undefined)[]>([undefined])
  const [pageIndex, setPageIndex] = useState(0)
  const [bookings, setBookings] = useState<Booking[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [rowBusy, setRowBusy] = useState<string | null>(null)
  const [rowError, setRowError] = useState<Record<string, string>>({})

  const cursor = cursorStack[pageIndex]

  const loadPage = useCallback(() => {
    setLoading(true)
    setError(null)
    const params: ListBookingsParams = { time: timeFilter }
    if (cursor !== undefined) {
      params.cursor = cursor
    }
    if (statusFilter !== 'all') {
      params.status = statusFilter
    }
    listBookings(params)
      .then((page) => {
        setBookings(page.data)
        setNextCursor(page.next_cursor)
      })
      .catch(() => {
        setError('Could not load your bookings.')
      })
      .finally(() => {
        setLoading(false)
      })
  }, [statusFilter, timeFilter, cursor])

  useEffect(() => {
    loadPage()
  }, [loadPage])

  function resetToFirstPage() {
    setCursorStack([undefined])
    setPageIndex(0)
  }

  function goNext() {
    if (!nextCursor) return
    setCursorStack((current) => [...current.slice(0, pageIndex + 1), nextCursor])
    setPageIndex((current) => current + 1)
  }

  function goPrevious() {
    setPageIndex((current) => Math.max(0, current - 1))
  }

  async function handleCancel(bookingId: string) {
    setRowBusy(bookingId)
    setRowError((current) => ({ ...current, [bookingId]: '' }))
    try {
      await cancelBooking(bookingId)
      loadPage()
    } catch (caught) {
      setRowError((current) => ({
        ...current,
        [bookingId]: caught instanceof ApiError ? caught.message : 'Could not cancel this booking.',
      }))
    } finally {
      setRowBusy(null)
    }
  }

  async function handleEditSave(bookingId: string, startValue: string, endValue: string) {
    setRowBusy(bookingId)
    setRowError((current) => ({ ...current, [bookingId]: '' }))
    try {
      await editBooking(bookingId, {
        start: fromDatetimeLocalValue(startValue).toISOString(),
        end: fromDatetimeLocalValue(endValue).toISOString(),
      })
      setEditingId(null)
      loadPage()
    } catch (caught) {
      // A 409 slot_unavailable here (the new range now conflicts with
      // something else) is the system working as designed, the same
      // Rollout v1.0 §7 exclusion documented in booking/CalendarPage.tsx —
      // it must never be reported to error tracking / paged to on-call.
      // The full "nearest open slots" treatment lives in the calendar's
      // own booking flow; here the backend's own message is shown as-is.
      setRowError((current) => ({
        ...current,
        [bookingId]: caught instanceof ApiError ? caught.message : 'Could not save this change.',
      }))
    } finally {
      setRowBusy(null)
    }
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">My Bookings</h1>

      <div className="mt-4 flex gap-3">
        <select
          value={statusFilter}
          onChange={(event) => {
            setStatusFilter(event.target.value as StatusFilter)
            resetToFirstPage()
          }}
          className="rounded-md border border-gray-300 px-2 py-1.5 text-sm"
          aria-label="Status"
        >
          <option value="all">All statuses</option>
          <option value="confirmed">Confirmed</option>
          <option value="cancelled">Cancelled</option>
        </select>
        <select
          value={timeFilter}
          onChange={(event) => {
            setTimeFilter(event.target.value as TimeFilter)
            resetToFirstPage()
          }}
          className="rounded-md border border-gray-300 px-2 py-1.5 text-sm"
          aria-label="Time"
        >
          <option value="upcoming">Upcoming</option>
          <option value="past">Past</option>
          <option value="all">All time</option>
        </select>
      </div>

      {loading && <p className="mt-4 text-sm text-gray-500">Loading…</p>}
      {error && (
        <p role="alert" className="mt-4 text-sm text-kairos-danger">
          {error}
        </p>
      )}

      <ul className="mt-4 divide-y divide-gray-200 rounded-md border border-gray-200 bg-white">
        {!loading && bookings.length === 0 && (
          <li className="p-4 text-sm text-gray-500">No bookings found.</li>
        )}
        {bookings.map((booking) => {
          const isEditing = editingId === booking.id
          const canManage = booking.status === 'confirmed'
          return (
            <li key={booking.id} className="p-4">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium text-gray-900">
                    {resourceNameById.get(booking.resource_id) ?? booking.resource_id}
                  </p>
                  {isEditing ? (
                    <EditRow
                      initialStart={booking.start}
                      initialEnd={booking.end}
                      busy={rowBusy === booking.id}
                      onCancel={() => {
                        setEditingId(null)
                      }}
                      onSave={(startValue, endValue) => {
                        void handleEditSave(booking.id, startValue, endValue)
                      }}
                    />
                  ) : (
                    <p className="text-sm text-gray-600">
                      {formatDateTime(booking.start, timeZone)} –{' '}
                      {formatDateTime(booking.end, timeZone)}
                    </p>
                  )}
                  <span
                    className={`mt-1 inline-block rounded-full px-2 py-0.5 text-xs ${
                      booking.status === 'cancelled'
                        ? 'bg-gray-200 text-gray-600'
                        : 'bg-green-100 text-green-700'
                    }`}
                  >
                    {booking.status}
                  </span>
                </div>
                {canManage && !isEditing && (
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => {
                        setEditingId(booking.id)
                      }}
                      className="rounded-md border border-gray-300 px-3 py-1.5 text-sm
                        text-gray-700 hover:bg-gray-50"
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      disabled={rowBusy === booking.id}
                      onClick={() => {
                        void handleCancel(booking.id)
                      }}
                      className="rounded-md bg-kairos-danger px-3 py-1.5 text-sm text-white
                        hover:opacity-90 disabled:opacity-50"
                    >
                      Cancel
                    </button>
                  </div>
                )}
              </div>
              {rowError[booking.id] && (
                <p role="alert" className="mt-2 text-sm text-kairos-danger">
                  {rowError[booking.id]}
                </p>
              )}
            </li>
          )
        })}
      </ul>

      <div className="mt-4 flex justify-between">
        <button
          type="button"
          disabled={pageIndex === 0}
          onClick={goPrevious}
          className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700
            hover:bg-gray-50 disabled:opacity-50"
        >
          Previous
        </button>
        <button
          type="button"
          disabled={!nextCursor}
          onClick={goNext}
          className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700
            hover:bg-gray-50 disabled:opacity-50"
        >
          Next
        </button>
      </div>
    </div>
  )
}

interface EditRowProps {
  initialStart: string
  initialEnd: string
  busy: boolean
  onCancel: () => void
  onSave: (startValue: string, endValue: string) => void
}

function EditRow({ initialStart, initialEnd, busy, onCancel, onSave }: EditRowProps) {
  const [startValue, setStartValue] = useState(toDatetimeLocalValue(new Date(initialStart)))
  const [endValue, setEndValue] = useState(toDatetimeLocalValue(new Date(initialEnd)))

  return (
    <div className="mt-1 flex flex-wrap items-center gap-2">
      <input
        type="datetime-local"
        value={startValue}
        onChange={(event) => {
          setStartValue(event.target.value)
        }}
        className="rounded-md border border-gray-300 px-2 py-1 text-sm"
      />
      <input
        type="datetime-local"
        value={endValue}
        onChange={(event) => {
          setEndValue(event.target.value)
        }}
        className="rounded-md border border-gray-300 px-2 py-1 text-sm"
      />
      <button
        type="button"
        disabled={busy}
        onClick={() => {
          onSave(startValue, endValue)
        }}
        className="rounded-md bg-kairos-primary px-2 py-1 text-sm text-white
          hover:bg-kairos-primary-dark disabled:opacity-50"
      >
        Save
      </button>
      <button
        type="button"
        onClick={onCancel}
        className="rounded-md border border-gray-300 px-2 py-1 text-sm text-gray-700
          hover:bg-gray-50"
      >
        Cancel
      </button>
    </div>
  )
}
