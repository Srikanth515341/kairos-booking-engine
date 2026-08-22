import { useState } from 'react'
import { deactivateUser } from '../api/admin'
import { ApiError } from '../api/errors'
import type { DeactivateUserResponse } from '../api/types'

export function OffboardingPage() {
  const [userId, setUserId] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<DeactivateUserResponse | null>(null)

  async function handleSubmit() {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      const outcome = await deactivateUser(userId.trim(), reason.trim())
      setResult(outcome)
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'permission_denied') {
        setError('You need system administrator access to deactivate a user.')
      } else if (caught instanceof ApiError) {
        setError(caught.message)
      } else {
        setError('Could not deactivate this user.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">User Offboarding</h1>
      <p className="mt-1 max-w-2xl text-sm text-gray-500">
        Deactivates a user and, per each affected resource&rsquo;s own offboarding policy,
        transfers, cancels-and-notifies, or retains their future bookings — releases any outstanding
        hold so it cascades to the next waitlisted person, cancels their waiting entries, and flags
        any active recurring series they own to that resource&rsquo;s admin. Repeating this on an
        already-deactivated user is a safe no-op.
      </p>

      <form
        className="mt-4 max-w-md space-y-3"
        onSubmit={(event) => {
          event.preventDefault()
          void handleSubmit()
        }}
      >
        <label className="block text-sm font-medium text-gray-700">
          User ID
          <input
            type="text"
            value={userId}
            onChange={(event) => {
              setUserId(event.target.value)
            }}
            placeholder="user UUID"
            className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
            required
          />
        </label>
        <label className="block text-sm font-medium text-gray-700">
          Reason
          <input
            type="text"
            value={reason}
            onChange={(event) => {
              setReason(event.target.value)
            }}
            placeholder="e.g. Employee offboarding — ticket HR-4821"
            className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
            required
          />
        </label>

        {error && (
          <p role="alert" className="text-sm text-kairos-danger">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy}
          className="rounded-md bg-kairos-danger px-4 py-2 text-sm font-medium text-white
            hover:opacity-90 disabled:opacity-50"
        >
          {busy ? 'Deactivating…' : 'Deactivate user'}
        </button>
      </form>

      {result && (
        <div role="status" className="mt-6 max-w-md rounded-md border border-gray-200 bg-white p-4">
          <p className="text-sm font-medium text-gray-900">
            User {result.user_id} is now {result.status}.
          </p>
          <dl className="mt-3 grid grid-cols-2 gap-3 text-sm">
            <div>
              <dt className="text-xs text-gray-500">Bookings transferred</dt>
              <dd className="font-medium text-gray-900">{result.bookings_transferred}</dd>
            </div>
            <div>
              <dt className="text-xs text-gray-500">Bookings cancelled &amp; notified</dt>
              <dd className="font-medium text-gray-900">{result.bookings_cancelled}</dd>
            </div>
            <div>
              <dt className="text-xs text-gray-500">Bookings retained</dt>
              <dd className="font-medium text-gray-900">{result.bookings_retained}</dd>
            </div>
            <div>
              <dt className="text-xs text-gray-500">Waitlist entries cancelled</dt>
              <dd className="font-medium text-gray-900">{result.waitlist_entries_cancelled}</dd>
            </div>
            <div>
              <dt className="text-xs text-gray-500">Offers released</dt>
              <dd className="font-medium text-gray-900">{result.offers_released}</dd>
            </div>
            <div>
              <dt className="text-xs text-gray-500">Series flagged for admin</dt>
              <dd className="font-medium text-gray-900">
                {result.series_flagged_for_admin.length}
              </dd>
            </div>
          </dl>
          {result.series_flagged_for_admin.length > 0 && (
            <ul className="mt-3 space-y-1 border-t border-gray-100 pt-3">
              {result.series_flagged_for_admin.map((series) => (
                <li key={series.series_id} className="text-xs text-gray-600">
                  Series {series.series_id} on resource {series.resource_id} —{' '}
                  {series.occurrences_remaining} occurrence
                  {series.occurrences_remaining === 1 ? '' : 's'} remaining, needs a resource
                  admin&rsquo;s decision (no automated transfer/termination exists for series yet).
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
