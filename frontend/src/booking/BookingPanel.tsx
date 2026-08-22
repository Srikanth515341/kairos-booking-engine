import { useState } from 'react'
import { fromDatetimeLocalValue, toDatetimeLocalValue } from './dateGrid'
import type { ConflictState, OpenSlotOption, Selection } from './selection'
import { formatDateTime } from './timezone'

interface BookingPanelProps {
  selection: Exclude<Selection, null>
  timeZone: string
  onClose: () => void
  onCreate: (start: Date, end: Date) => void
  onCancel: (bookingId: string, reason: string | undefined) => void
  onEdit: (bookingId: string, start: Date, end: Date) => void
  onPickSuggestedSlot: (slot: OpenSlotOption) => void
  conflict: ConflictState | null
  actionError: string | null
  busy: boolean
}

export function BookingPanel({
  selection,
  timeZone,
  onClose,
  onCreate,
  onCancel,
  onEdit,
  onPickSuggestedSlot,
  conflict,
  actionError,
  busy,
}: BookingPanelProps) {
  const initialStart = selection.kind === 'new' ? selection.start : new Date(selection.block.start)
  const initialEnd = selection.kind === 'new' ? selection.end : new Date(selection.block.end)
  const [startValue, setStartValue] = useState(toDatetimeLocalValue(initialStart))
  const [endValue, setEndValue] = useState(toDatetimeLocalValue(initialEnd))
  const [editing, setEditing] = useState(false)
  const [reason, setReason] = useState('')

  return (
    <aside className="w-full max-w-sm shrink-0 rounded-md border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-900">
          {selection.kind === 'new' ? 'New booking' : 'Manage booking'}
        </h2>
        <button
          type="button"
          onClick={onClose}
          className="text-sm text-gray-400 hover:text-gray-600"
          aria-label="Close"
        >
          ×
        </button>
      </div>

      {selection.kind === 'manage' && selection.block.owner && (
        <p className="mt-2 text-sm text-gray-600">
          Booked by <span className="font-medium">{selection.block.owner.display_name}</span>
        </p>
      )}

      {selection.kind === 'manage' && !editing ? (
        <div className="mt-3 space-y-3">
          <p className="text-sm text-gray-700">
            {formatDateTime(selection.block.start, timeZone)} –{' '}
            {formatDateTime(selection.block.end, timeZone)}
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => {
                setEditing(true)
              }}
              className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700
                hover:bg-gray-50"
            >
              Edit
            </button>
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={reason}
                onChange={(event) => {
                  setReason(event.target.value)
                }}
                placeholder="Reason (if not your booking)"
                className="w-40 rounded-md border border-gray-300 px-2 py-1.5 text-sm"
              />
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  onCancel(selection.bookingId, reason.trim() || undefined)
                }}
                className="rounded-md bg-kairos-danger px-3 py-1.5 text-sm text-white
                  hover:opacity-90 disabled:opacity-50"
              >
                Cancel booking
              </button>
            </div>
          </div>
        </div>
      ) : (
        <form
          className="mt-3 space-y-3"
          onSubmit={(event) => {
            event.preventDefault()
            const start = fromDatetimeLocalValue(startValue)
            const end = fromDatetimeLocalValue(endValue)
            if (selection.kind === 'new') {
              onCreate(start, end)
            } else {
              onEdit(selection.bookingId, start, end)
            }
          }}
        >
          <label className="block text-xs font-medium text-gray-600">
            Start
            <input
              type="datetime-local"
              value={startValue}
              onChange={(event) => {
                setStartValue(event.target.value)
              }}
              className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
              required
            />
          </label>
          <label className="block text-xs font-medium text-gray-600">
            End
            <input
              type="datetime-local"
              value={endValue}
              onChange={(event) => {
                setEndValue(event.target.value)
              }}
              className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
              required
            />
          </label>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={busy}
              className="rounded-md bg-kairos-primary px-3 py-1.5 text-sm text-white
                hover:bg-kairos-primary-dark disabled:opacity-50"
            >
              {selection.kind === 'new' ? 'Book' : 'Save'}
            </button>
            {selection.kind === 'manage' && (
              <button
                type="button"
                onClick={() => {
                  setEditing(false)
                }}
                className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700
                  hover:bg-gray-50"
              >
                Back
              </button>
            )}
          </div>
        </form>
      )}

      {actionError && (
        <p role="alert" className="mt-3 text-sm text-kairos-danger">
          {actionError}
        </p>
      )}

      {conflict && (
        <div role="alert" className="mt-3 rounded-md bg-amber-50 p-3">
          <p className="text-sm text-amber-800">{conflict.message}</p>
          {conflict.nearestSlots.length > 0 && (
            <ul className="mt-2 space-y-1">
              {conflict.nearestSlots.map((slot) => (
                <li key={slot.start}>
                  <button
                    type="button"
                    onClick={() => {
                      onPickSuggestedSlot(slot)
                      setStartValue(toDatetimeLocalValue(new Date(slot.start)))
                      setEndValue(toDatetimeLocalValue(new Date(slot.end)))
                    }}
                    className="text-sm font-medium text-kairos-primary underline
                      hover:text-kairos-primary-dark"
                  >
                    {formatDateTime(slot.start, timeZone)} – {formatDateTime(slot.end, timeZone)}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </aside>
  )
}
