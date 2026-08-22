import type { RecurringPreviewResponse } from '../api/types'
import { formatDateTime } from '../booking/timezone'
import { trimSeconds } from './weekday'

interface PreviewPanelProps {
  preview: RecurringPreviewResponse
  timeZone: string
  acknowledgedConflicts: Set<string>
  acknowledgedAdjustments: Set<string>
  onToggleConflict: (occurrenceDate: string) => void
  onToggleAdjustment: (occurrenceDate: string) => void
  onAcknowledgeAllConflicts: () => void
  onAcknowledgeAllAdjustments: () => void
  onConfirm: () => void
  onBack: () => void
  confirmBusy: boolean
  confirmError: string | null
}

export function PreviewPanel({
  preview,
  timeZone,
  acknowledgedConflicts,
  acknowledgedAdjustments,
  onToggleConflict,
  onToggleAdjustment,
  onAcknowledgeAllConflicts,
  onAcknowledgeAllAdjustments,
  onConfirm,
  onBack,
  confirmBusy,
  confirmError,
}: PreviewPanelProps) {
  const allConflictsAcknowledged = preview.conflicts.every((conflict) =>
    acknowledgedConflicts.has(conflict.occurrence_date),
  )
  const allAdjustmentsAcknowledged = preview.time_adjustments.every((adjustment) =>
    acknowledgedAdjustments.has(adjustment.occurrence_date),
  )
  // Spec v1.0 §10 / PRD FR33: confirm must stay disabled until the user
  // has actually seen and acknowledged every conflict AND every time
  // adjustment — nothing here pre-checks these boxes on the user's
  // behalf.
  const canConfirm = allConflictsAcknowledged && allAdjustmentsAcknowledged

  return (
    <div className="max-w-2xl space-y-6">
      <h1 className="text-2xl font-semibold text-gray-900">Review this series</h1>

      <section>
        <h2 className="text-sm font-semibold text-gray-900">
          Will be created ({preview.would_create.length})
        </h2>
        <ul className="mt-2 space-y-1">
          {preview.would_create.map((item) => (
            <li
              key={item.occurrence_date}
              className="rounded-md bg-green-50 px-3 py-1.5 text-sm text-green-800"
            >
              {formatDateTime(item.start, timeZone)} – {formatDateTime(item.end, timeZone)}
            </li>
          ))}
        </ul>
      </section>

      {preview.conflicts.length > 0 && (
        <section>
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-900">
              Conflicts — will NOT be created ({preview.conflicts.length})
            </h2>
            {!allConflictsAcknowledged && (
              <button
                type="button"
                onClick={onAcknowledgeAllConflicts}
                className="text-xs font-medium text-kairos-primary underline"
              >
                Acknowledge all
              </button>
            )}
          </div>
          <ul className="mt-2 space-y-1">
            {preview.conflicts.map((conflict) => (
              <li
                key={conflict.occurrence_date}
                className="flex items-center gap-2 rounded-md bg-amber-50 px-3 py-1.5 text-sm
                  text-amber-800"
              >
                <input
                  type="checkbox"
                  checked={acknowledgedConflicts.has(conflict.occurrence_date)}
                  onChange={() => {
                    onToggleConflict(conflict.occurrence_date)
                  }}
                  aria-label={`Acknowledge conflict on ${conflict.occurrence_date}`}
                />
                <span>
                  {formatDateTime(conflict.start, timeZone)} –{' '}
                  {formatDateTime(conflict.end, timeZone)} — already booked
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {preview.time_adjustments.length > 0 && (
        <section>
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-900">
              Time adjustments — daylight saving transitions ({preview.time_adjustments.length})
            </h2>
            {!allAdjustmentsAcknowledged && (
              <button
                type="button"
                onClick={onAcknowledgeAllAdjustments}
                className="text-xs font-medium text-kairos-primary underline"
              >
                Acknowledge all
              </button>
            )}
          </div>
          <ul className="mt-2 space-y-1">
            {preview.time_adjustments.map((adjustment) => (
              <li
                key={adjustment.occurrence_date}
                className="flex items-start gap-2 rounded-md bg-blue-50 px-3 py-1.5 text-sm
                  text-blue-900"
              >
                <input
                  type="checkbox"
                  checked={acknowledgedAdjustments.has(adjustment.occurrence_date)}
                  onChange={() => {
                    onToggleAdjustment(adjustment.occurrence_date)
                  }}
                  aria-label={`Acknowledge time adjustment on ${adjustment.occurrence_date}`}
                  className="mt-0.5"
                />
                <span>
                  <strong>{adjustment.occurrence_date}</strong>:{' '}
                  {adjustment.issue === 'nonexistent_local_time'
                    ? `${trimSeconds(adjustment.requested_local)} does not exist that day (clocks skip forward) — shifted to ${trimSeconds(adjustment.adjusted_local)}.`
                    : `${trimSeconds(adjustment.requested_local)} occurs twice that day (clocks fall back) — the earlier occurrence is used.`}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {confirmError && (
        <p role="alert" className="text-sm text-kairos-danger">
          {confirmError}
        </p>
      )}

      <div className="flex gap-2">
        <button
          type="button"
          onClick={onBack}
          className="rounded-md border border-gray-300 px-4 py-2 text-sm text-gray-700
            hover:bg-gray-50"
        >
          Back
        </button>
        <button
          type="button"
          disabled={!canConfirm || confirmBusy}
          onClick={onConfirm}
          className="rounded-md bg-kairos-primary px-4 py-2 text-sm font-medium text-white
            hover:bg-kairos-primary-dark disabled:opacity-50"
        >
          {confirmBusy ? 'Confirming…' : 'Confirm series'}
        </button>
      </div>
    </div>
  )
}
