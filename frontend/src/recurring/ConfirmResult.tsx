import { useState } from 'react'
import type { RecurringConfirmResponse, RecurringSeriesCancelResponse } from '../api/types'
import { formatDateTime } from '../booking/timezone'

interface ConfirmResultProps {
  result: RecurringConfirmResponse
  timeZone: string
  onCancelSeries: (reason: string | undefined) => Promise<RecurringSeriesCancelResponse>
  onStartNew: () => void
}

// Spec v1.0 §10 / Rollout §7-style discipline: a 207 with a non-empty
// `conflicts` array is a SUCCESS, not a partial failure — this component
// never renders an error state for it, only per-occurrence detail.
export function ConfirmResult({
  result,
  timeZone,
  onCancelSeries,
  onStartNew,
}: ConfirmResultProps) {
  const [cancelling, setCancelling] = useState(false)
  const [cancelResult, setCancelResult] = useState<RecurringSeriesCancelResponse | null>(null)
  const [cancelError, setCancelError] = useState<string | null>(null)

  const acknowledgedConflicts = result.conflicts.filter((conflict) => conflict.acknowledged)
  const unacknowledgedConflicts = result.conflicts.filter((conflict) => !conflict.acknowledged)

  async function handleCancelSeries() {
    setCancelling(true)
    setCancelError(null)
    try {
      const cancelled = await onCancelSeries(undefined)
      setCancelResult(cancelled)
    } catch (caught) {
      setCancelError(caught instanceof Error ? caught.message : 'Could not cancel this series.')
    } finally {
      setCancelling(false)
    }
  }

  return (
    <div className="max-w-2xl space-y-6">
      <h1 className="text-2xl font-semibold text-gray-900">Series confirmed</h1>
      <p className="text-sm text-gray-600">
        {result.created.length} occurrence{result.created.length === 1 ? '' : 's'} created
        {result.conflicts.length > 0 ? `, ${String(result.conflicts.length)} not created.` : '.'}
      </p>

      {result.created.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-gray-900">Created</h2>
          <ul className="mt-2 space-y-1">
            {result.created.map((item) => (
              <li
                key={item.booking_id}
                className="rounded-md bg-green-50 px-3 py-1.5 text-sm text-green-800"
              >
                {formatDateTime(item.start, timeZone)} – {formatDateTime(item.end, timeZone)}
              </li>
            ))}
          </ul>
        </section>
      )}

      {unacknowledgedConflicts.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-gray-900">
            Not created — taken while you were confirming ({unacknowledgedConflicts.length})
          </h2>
          <ul className="mt-2 space-y-1">
            {unacknowledgedConflicts.map((conflict) => (
              <li
                key={conflict.occurrence_date}
                className="rounded-md border border-kairos-danger bg-red-50 px-3 py-1.5 text-sm
                  text-kairos-danger"
              >
                <strong>{conflict.occurrence_date}</strong> — this slot was taken while you were
                confirming.
              </li>
            ))}
          </ul>
        </section>
      )}

      {acknowledgedConflicts.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-gray-900">
            Not created — conflicts you already saw ({acknowledgedConflicts.length})
          </h2>
          <ul className="mt-2 space-y-1">
            {acknowledgedConflicts.map((conflict) => (
              <li
                key={conflict.occurrence_date}
                className="rounded-md bg-gray-100 px-3 py-1.5 text-sm text-gray-600"
              >
                <strong>{conflict.occurrence_date}</strong> — skipped, as acknowledged in preview.
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="border-t border-gray-200 pt-4">
        {cancelResult ? (
          <p className="text-sm text-gray-600">
            Series cancelled — {cancelResult.cancelled_booking_ids.length} future occurrence
            {cancelResult.cancelled_booking_ids.length === 1 ? '' : 's'} cancelled
            {cancelResult.occurrences_already_past > 0
              ? `, ${String(cancelResult.occurrences_already_past)} already-past occurrence${
                  cancelResult.occurrences_already_past === 1 ? '' : 's'
                } left untouched.`
              : '.'}
          </p>
        ) : (
          <>
            <button
              type="button"
              disabled={cancelling}
              onClick={() => {
                void handleCancelSeries()
              }}
              className="rounded-md bg-kairos-danger px-4 py-2 text-sm text-white hover:opacity-90
                disabled:opacity-50"
            >
              {cancelling ? 'Cancelling…' : 'Cancel this series'}
            </button>
            {cancelError && (
              <p role="alert" className="mt-2 text-sm text-kairos-danger">
                {cancelError}
              </p>
            )}
          </>
        )}
      </section>

      <button
        type="button"
        onClick={onStartNew}
        className="rounded-md border border-gray-300 px-4 py-2 text-sm text-gray-700
          hover:bg-gray-50"
      >
        Book another series
      </button>
    </div>
  )
}
