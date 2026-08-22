import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { getBookingHistory } from '../api/admin'
import { ApiError } from '../api/errors'
import type { BookingHistoryEvent } from '../api/types'
import { formatDateTime, getViewerTimeZone } from './timezone'

const ACTOR_TYPE_LABELS: Record<BookingHistoryEvent['actor']['type'], string> = {
  user: 'User',
  admin: 'Admin override',
  system: 'System (automated)',
  unknown: 'Unknown',
}

function formatChangeValue(value: unknown): string {
  if (value === null) return '—'
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  return JSON.stringify(value)
}

interface HistoryState {
  /** The booking id this result actually belongs to — compared against
   * the CURRENT `id` param to derive `loading`, the same fetch-key
   * pattern `booking/useAvailability.ts` already established, rather
   * than a separate `loading` boolean needing its own synchronous
   * `setState` call inside the effect body
   * (`react-hooks/set-state-in-effect`). */
  key: string
  events: BookingHistoryEvent[] | null
  error: string | null
}

export function BookingHistoryPage() {
  const { id } = useParams<{ id: string }>()
  const timeZone = getViewerTimeZone()

  const [state, setState] = useState<HistoryState>({ key: '', events: null, error: null })

  useEffect(() => {
    if (!id) return
    let cancelled = false
    getBookingHistory(id)
      .then((response) => {
        if (!cancelled) setState({ key: id, events: response.events, error: null })
      })
      .catch((caught: unknown) => {
        if (cancelled) return
        // A 404 here means EITHER the booking doesn't exist OR the
        // viewer isn't its owner/a resource admin/operations — the same
        // existence-hiding opacity every other booking endpoint in this
        // API already uses (never a 403, so a stranger can't distinguish
        // "doesn't exist" from "exists but isn't visible to you").
        const message =
          caught instanceof ApiError && caught.code === 'not_found'
            ? 'This booking does not exist, or you do not have permission to view it.'
            : 'Could not load this booking’s history.'
        setState({ key: id, events: null, error: message })
      })
    return () => {
      cancelled = true
    }
  }, [id])

  const loading = id !== undefined && state.key !== id
  const events = loading ? null : state.events
  const error = loading ? null : state.error

  return (
    <div>
      <Link to="/bookings" className="text-sm text-kairos-primary hover:underline">
        ← Back to My Bookings
      </Link>
      <h1 className="mt-2 text-2xl font-semibold text-gray-900">Booking history</h1>
      <p className="mt-1 text-sm text-gray-500">Booking {id}</p>

      {loading && <p className="mt-4 text-sm text-gray-500">Loading…</p>}
      {error && (
        <p role="alert" className="mt-4 text-sm text-kairos-danger">
          {error}
        </p>
      )}

      {events && (
        <ol className="mt-4 space-y-3 border-l border-gray-200 pl-4">
          {events.map((event, index) => (
            <li key={`${event.occurred_at}-${String(index)}`} className="relative">
              <span className="absolute -left-[21px] top-1.5 h-2 w-2 rounded-full bg-kairos-primary" />
              <p className="text-sm font-medium text-gray-900">
                {formatDateTime(event.occurred_at, timeZone)} —{' '}
                {event.action === 'insert' ? 'Created' : 'Updated'}
              </p>
              <p className="text-sm text-gray-600">
                {ACTOR_TYPE_LABELS[event.actor.type]}
                {event.actor.display_name ? `: ${event.actor.display_name}` : ''}
              </p>
              {event.reason && (
                <p className="mt-1 text-sm text-gray-700">
                  Reason: <span className="italic">{event.reason}</span>
                </p>
              )}
              {Object.keys(event.changes).length > 0 && (
                <ul className="mt-1 space-y-0.5 text-xs text-gray-500">
                  {Object.entries(event.changes).map(([field, [before, after]]) => (
                    <li key={field}>
                      <span className="font-mono">{field}</span>: {formatChangeValue(before)} →{' '}
                      {formatChangeValue(after)}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
          {events.length === 0 && <li className="text-sm text-gray-500">No history recorded.</li>}
        </ol>
      )}
    </div>
  )
}
