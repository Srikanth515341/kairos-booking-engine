import { useState } from 'react'
import { ApiError } from '../api/errors'
import type { WaitlistEntry } from '../api/types'
import { joinWaitlist } from '../api/waitlist'
import { formatDateTime } from '../booking/timezone'

interface JoinWaitlistPanelProps {
  resourceId: string
  start: Date
  end: Date
  timeZone: string
  onClose: () => void
  /** Only fired for a 422 `slot_already_available` — hands the exact
   * range back to the caller so it can open the ordinary booking flow
   * pre-filled, rather than making the user re-click the grid. */
  onBookInstead: (start: Date, end: Date) => void
}

export function JoinWaitlistPanel({
  resourceId,
  start,
  end,
  timeZone,
  onClose,
  onBookInstead,
}: JoinWaitlistPanelProps) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [slotNowAvailable, setSlotNowAvailable] = useState(false)
  const [alreadyWaitlisted, setAlreadyWaitlisted] = useState(false)
  const [joinedEntry, setJoinedEntry] = useState<WaitlistEntry | null>(null)

  async function handleJoin() {
    setBusy(true)
    setError(null)
    setSlotNowAvailable(false)
    setAlreadyWaitlisted(false)
    try {
      const entry = await joinWaitlist({
        resource_id: resourceId,
        start: start.toISOString(),
        end: end.toISOString(),
      })
      setJoinedEntry(entry)
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'slot_already_available') {
        // Advisory check-then-act (Spec v1.0 §5.11): the range became
        // bookable directly between opening this panel and clicking
        // Join — handled gracefully, not as an error (Phase 26 Scope IN).
        setSlotNowAvailable(true)
      } else if (caught instanceof ApiError && caught.code === 'already_on_waitlist') {
        setAlreadyWaitlisted(true)
      } else if (caught instanceof ApiError) {
        setError(caught.message)
      } else {
        setError('Could not join the waitlist. Please try again.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <aside className="w-full max-w-sm shrink-0 rounded-md border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-900">Join waitlist</h2>
        <button
          type="button"
          onClick={onClose}
          className="text-sm text-gray-400 hover:text-gray-600"
          aria-label="Close"
        >
          ×
        </button>
      </div>

      <p className="mt-2 text-sm text-gray-700">
        {formatDateTime(start.toISOString(), timeZone)} –{' '}
        {formatDateTime(end.toISOString(), timeZone)}
      </p>

      {/* Phase 26 Scope IN: the containment eligibility rule (PRD FR21)
          explained where the user will actually see it, at the moment
          they're deciding whether to join. */}
      <p className="mt-3 rounded-md bg-blue-50 p-2 text-xs text-blue-900">
        You will only be offered this slot if the ENTIRE freed time covers your full request. A
        smaller freed window does not qualify — for example, if you request 10:00–11:00, a freed
        10:00–10:30 will not be offered to you.
      </p>

      {joinedEntry && (
        <div role="status" className="mt-3 rounded-md bg-green-50 p-3 text-sm text-green-800">
          You&rsquo;re on the waitlist
          {joinedEntry.queue_position !== null
            ? ` — position #${String(joinedEntry.queue_position)}.`
            : '.'}
          <button
            type="button"
            onClick={onClose}
            className="mt-2 block rounded-md bg-kairos-primary px-3 py-1.5 text-sm text-white
              hover:bg-kairos-primary-dark"
          >
            Done
          </button>
        </div>
      )}

      {slotNowAvailable && (
        <div role="status" className="mt-3 rounded-md bg-green-50 p-3 text-sm text-green-800">
          Good news — this slot is available right now. You can book it directly instead of waiting.
          <button
            type="button"
            onClick={() => {
              onBookInstead(start, end)
            }}
            className="mt-2 block rounded-md bg-kairos-primary px-3 py-1.5 text-sm text-white
              hover:bg-kairos-primary-dark"
          >
            Book this slot
          </button>
        </div>
      )}

      {alreadyWaitlisted && (
        <p role="status" className="mt-3 text-sm text-gray-600">
          You&rsquo;re already on the waitlist for this time.
        </p>
      )}

      {error && (
        <p role="alert" className="mt-3 text-sm text-kairos-danger">
          {error}
        </p>
      )}

      {!joinedEntry && !slotNowAvailable && !alreadyWaitlisted && (
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            void handleJoin()
          }}
          className="mt-3 rounded-md bg-kairos-primary px-4 py-2 text-sm font-medium text-white
            hover:bg-kairos-primary-dark disabled:opacity-50"
        >
          {busy ? 'Joining…' : 'Join waitlist'}
        </button>
      )}
    </aside>
  )
}
