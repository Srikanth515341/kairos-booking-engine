import { useCallback, useEffect, useState } from 'react'
import { useResources } from '../booking/useResources'
import { formatDateTime, getViewerTimeZone } from '../booking/timezone'
import { ApiError } from '../api/errors'
import type { WaitlistEntry, WaitlistEntryStatus } from '../api/types'
import {
  cancelWaitlistEntry,
  confirmWaitlistOffer,
  declineWaitlistOffer,
  listWaitlistEntries,
  type ListWaitlistEntriesParams,
} from '../api/waitlist'
import { OfferCountdown } from './OfferCountdown'

type StatusFilter = 'all' | WaitlistEntryStatus

const STATUS_LABELS: Record<WaitlistEntryStatus, string> = {
  waiting: 'Waiting',
  offered: 'Offer available',
  fulfilled: 'Booked',
  expired: 'Offer expired',
  cancelled: 'Cancelled',
}

const STATUS_BADGE_CLASSES: Record<WaitlistEntryStatus, string> = {
  waiting: 'bg-blue-100 text-blue-700',
  offered: 'bg-amber-100 text-amber-800',
  fulfilled: 'bg-green-100 text-green-700',
  expired: 'bg-gray-200 text-gray-600',
  cancelled: 'bg-gray-200 text-gray-600',
}

export function MyWaitlistPage() {
  const { resources } = useResources()
  const resourceNameById = new Map(resources.map((resource) => [resource.id, resource.name]))
  const timeZone = getViewerTimeZone()

  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [cursorStack, setCursorStack] = useState<(string | undefined)[]>([undefined])
  const [pageIndex, setPageIndex] = useState(0)
  const [entries, setEntries] = useState<WaitlistEntry[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [rowBusy, setRowBusy] = useState<string | null>(null)
  // Kept as two SEPARATE records, not one — a genuine failure (role="alert",
  // this app's established convention for real errors) must never share a
  // slot with an expected, non-error outcome like offer_expired or a
  // success confirmation (role="status"). Setting one always clears the
  // other for the same row.
  const [rowError, setRowError] = useState<Record<string, string>>({})
  const [rowStatus, setRowStatus] = useState<Record<string, string>>({})

  const cursor = cursorStack[pageIndex]

  const loadPage = useCallback(() => {
    setLoading(true)
    setError(null)
    const params: ListWaitlistEntriesParams = {}
    if (cursor !== undefined) {
      params.cursor = cursor
    }
    if (statusFilter !== 'all') {
      params.status = statusFilter
    }
    listWaitlistEntries(params)
      .then((page) => {
        setEntries(page.data)
        setNextCursor(page.next_cursor)
      })
      .catch(() => {
        setError('Could not load your waitlist entries.')
      })
      .finally(() => {
        setLoading(false)
      })
  }, [statusFilter, cursor])

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

  function clearRowMessages(entryId: string) {
    setRowError((current) => ({ ...current, [entryId]: '' }))
    setRowStatus((current) => ({ ...current, [entryId]: '' }))
  }

  async function handleCancel(entryId: string) {
    setRowBusy(entryId)
    clearRowMessages(entryId)
    try {
      await cancelWaitlistEntry(entryId)
      loadPage()
    } catch (caught) {
      setRowError((current) => ({
        ...current,
        [entryId]: caught instanceof ApiError ? caught.message : 'Could not cancel this entry.',
      }))
    } finally {
      setRowBusy(null)
    }
  }

  async function handleConfirmOffer(entryId: string, offerId: string) {
    setRowBusy(entryId)
    clearRowMessages(entryId)
    try {
      await confirmWaitlistOffer(offerId)
      setRowStatus((current) => ({ ...current, [entryId]: 'Booked! Check My Bookings.' }))
      loadPage()
    } catch (caught) {
      // A 409 offer_expired here is an EXPECTED outcome under ordinary
      // network latency (Spec v1.0 §5.13), never rendered as an error —
      // role="status", not role="alert" — the entry is simply refreshed
      // so the user sees current state.
      if (caught instanceof ApiError && caught.code === 'offer_expired') {
        setRowStatus((current) => ({
          ...current,
          [entryId]: 'This offer has expired. Refreshing your waitlist status…',
        }))
        loadPage()
      } else {
        setRowError((current) => ({
          ...current,
          [entryId]: caught instanceof ApiError ? caught.message : 'Could not confirm this offer.',
        }))
      }
    } finally {
      setRowBusy(null)
    }
  }

  async function handleDeclineOffer(entryId: string, offerId: string) {
    setRowBusy(entryId)
    clearRowMessages(entryId)
    try {
      await declineWaitlistOffer(offerId)
      setRowStatus((current) => ({
        ...current,
        [entryId]: 'Declined — this slot has been offered to the next person in line.',
      }))
      loadPage()
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'offer_already_resolved') {
        setRowStatus((current) => ({
          ...current,
          [entryId]: 'This offer was already resolved. Refreshing your waitlist status…',
        }))
        loadPage()
      } else {
        setRowError((current) => ({
          ...current,
          [entryId]: caught instanceof ApiError ? caught.message : 'Could not decline this offer.',
        }))
      }
    } finally {
      setRowBusy(null)
    }
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">My Waitlist</h1>

      {/* PRD FR21 eligibility rule, explained where the user browsing
          their whole waitlist will see it too — not only at join time. */}
      <p className="mt-2 max-w-2xl rounded-md bg-blue-50 p-3 text-sm text-blue-900">
        You&rsquo;re offered a freed slot only when the ENTIRE freed time covers your full request —
        a smaller freed window never qualifies. Example: waitlisting 10:00–11:00 means a freed
        10:00–10:30 will not trigger an offer for you.
      </p>

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
          <option value="waiting">Waiting</option>
          <option value="offered">Offer available</option>
          <option value="fulfilled">Booked</option>
          <option value="expired">Offer expired</option>
          <option value="cancelled">Cancelled</option>
        </select>
      </div>

      {loading && <p className="mt-4 text-sm text-gray-500">Loading…</p>}
      {error && (
        <p role="alert" className="mt-4 text-sm text-kairos-danger">
          {error}
        </p>
      )}

      <ul className="mt-4 divide-y divide-gray-200 rounded-md border border-gray-200 bg-white">
        {!loading && entries.length === 0 && (
          <li className="p-4 text-sm text-gray-500">No waitlist entries found.</li>
        )}
        {entries.map((entry) => (
          <li key={entry.id} className="p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-900">
                  {resourceNameById.get(entry.resource_id) ?? entry.resource_id}
                </p>
                <p className="text-sm text-gray-600">
                  {formatDateTime(entry.start, timeZone)} – {formatDateTime(entry.end, timeZone)}
                </p>
                <div className="mt-1 flex items-center gap-2">
                  <span
                    className={`inline-block rounded-full px-2 py-0.5 text-xs ${STATUS_BADGE_CLASSES[entry.status]}`}
                  >
                    {STATUS_LABELS[entry.status]}
                  </span>
                  {entry.status === 'waiting' && entry.queue_position !== null && (
                    <span className="text-xs text-gray-500">Position #{entry.queue_position}</span>
                  )}
                </div>
              </div>
              <div className="flex flex-col items-end gap-2">
                {entry.status === 'waiting' && (
                  <button
                    type="button"
                    disabled={rowBusy === entry.id}
                    onClick={() => {
                      void handleCancel(entry.id)
                    }}
                    className="rounded-md border border-gray-300 px-3 py-1.5 text-sm
                      text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                  >
                    Leave waitlist
                  </button>
                )}
                {entry.status === 'offered' && entry.active_offer && (
                  <div className="flex flex-col items-end gap-1">
                    <OfferCountdown expiresAt={entry.active_offer.expires_at} />
                    <div className="flex gap-2">
                      <button
                        type="button"
                        disabled={rowBusy === entry.id}
                        onClick={() => {
                          if (entry.active_offer) {
                            void handleConfirmOffer(entry.id, entry.active_offer.id)
                          }
                        }}
                        className="rounded-md bg-kairos-primary px-3 py-1.5 text-sm text-white
                          hover:bg-kairos-primary-dark disabled:opacity-50"
                      >
                        Confirm
                      </button>
                      <button
                        type="button"
                        disabled={rowBusy === entry.id}
                        onClick={() => {
                          if (entry.active_offer) {
                            void handleDeclineOffer(entry.id, entry.active_offer.id)
                          }
                        }}
                        className="rounded-md border border-gray-300 px-3 py-1.5 text-sm
                          text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                      >
                        Decline
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </div>
            {rowStatus[entry.id] && (
              <p role="status" className="mt-2 text-sm text-gray-600">
                {rowStatus[entry.id]}
              </p>
            )}
            {rowError[entry.id] && (
              <p role="alert" className="mt-2 text-sm text-kairos-danger">
                {rowError[entry.id]}
              </p>
            )}
          </li>
        ))}
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
