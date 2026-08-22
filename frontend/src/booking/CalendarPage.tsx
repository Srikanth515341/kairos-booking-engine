import { useMemo, useState } from 'react'
import { cancelBooking, createBooking, editBooking } from '../api/bookings'
import { ApiError } from '../api/errors'
import type { Booking } from '../api/types'
import { JoinWaitlistPanel } from '../waitlist/JoinWaitlistPanel'
import { BookingPanel } from './BookingPanel'
import { findNearestOpenSlots, slotUnavailableMessage } from './conflicts'
import { daysInView, rangeForView, stepAnchor, type CalendarView } from './dateGrid'
import type { DisplayBlock } from './DisplayBlock'
import { MonthGrid } from './MonthGrid'
import type { ConflictState, OpenSlotOption, Selection } from './selection'
import { TimeGrid } from './TimeGrid'
import { formatDateLabel, getViewerTimeZone, zoneShortLabel } from './timezone'
import { useAvailability } from './useAvailability'
import { useResources } from './useResources'

function bookingToDisplayBlock(booking: Booking): DisplayBlock {
  return { start: booking.start, end: booking.end, booking_id: booking.id }
}

const VIEWS: CalendarView[] = ['day', 'week', 'month']

export function CalendarPage() {
  const { resources, loading: resourcesLoading } = useResources()
  const [selectedResourceId, setSelectedResourceId] = useState<string | null>(null)
  const [view, setView] = useState<CalendarView>('week')
  const [anchor, setAnchor] = useState(new Date())
  const [selection, setSelection] = useState<Selection>(null)
  const [waitlistSelection, setWaitlistSelection] = useState<{ start: Date; end: Date } | null>(
    null,
  )
  const [pendingBlock, setPendingBlock] = useState<DisplayBlock | null>(null)
  const [justCreated, setJustCreated] = useState<DisplayBlock[]>([])
  const [conflict, setConflict] = useState<ConflictState | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const timeZone = getViewerTimeZone()

  // Derived, not synced via an effect: once resources load, the first one
  // is the default until the user explicitly picks another via the
  // <select> below (which sets selectedResourceId directly).
  const resourceId = selectedResourceId ?? resources[0]?.id ?? null

  const { from, to } = useMemo(() => rangeForView(view, anchor), [view, anchor])
  const fromIso = from.toISOString()
  const toIso = to.toISOString()
  const days = useMemo(() => daysInView(view, anchor), [view, anchor])

  const {
    data: availability,
    loading: availabilityLoading,
    error,
    refetch,
  } = useAvailability(resourceId, fromIso, toIso)

  // Any change to what's being viewed invalidates in-flight UI state from
  // the previous view — a stale conflict banner or a "just created" block
  // for a slot that's no longer on screen would be actively misleading.
  // This resets it during RENDER (React's own documented "adjusting state
  // when a prop changes" pattern — https://react.dev/learn/you-might-not-need-an-effect),
  // not inside a useEffect, since the reset must be visible in the SAME
  // commit as the view change, not one render later.
  const viewKey = `${resourceId ?? ''}|${view}|${String(anchor.getTime())}`
  const [lastViewKey, setLastViewKey] = useState(viewKey)
  if (viewKey !== lastViewKey) {
    setLastViewKey(viewKey)
    setSelection(null)
    setWaitlistSelection(null)
    setPendingBlock(null)
    setJustCreated([])
    setConflict(null)
    setActionError(null)
  }

  const displayBlocks: DisplayBlock[] = useMemo(() => {
    const serverBlocks = availability?.busy_blocks ?? []
    return [...serverBlocks, ...justCreated, ...(pendingBlock ? [pendingBlock] : [])]
  }, [availability, justCreated, pendingBlock])

  function handleSlotClick(start: Date, end: Date) {
    setConflict(null)
    setActionError(null)
    setWaitlistSelection(null)
    setSelection({ kind: 'new', start, end })
  }

  function handleBlockClick(block: DisplayBlock) {
    if (!block.booking_id) return
    setConflict(null)
    setActionError(null)
    setWaitlistSelection(null)
    setSelection({ kind: 'manage', bookingId: block.booking_id, block })
  }

  function handleBusyBlockClick(start: Date, end: Date) {
    setSelection(null)
    setWaitlistSelection({ start, end })
  }

  function handleBookInsteadFromWaitlist(start: Date, end: Date) {
    setWaitlistSelection(null)
    setConflict(null)
    setActionError(null)
    setSelection({ kind: 'new', start, end })
  }

  function currentBusyBlocksForConflictSearch(excludingStartIso: string, excludingEndIso: string) {
    return [
      ...(availability?.busy_blocks ?? []),
      ...justCreated,
      { start: excludingStartIso, end: excludingEndIso },
    ]
  }

  async function handleCreate(start: Date, end: Date) {
    if (!resourceId) return
    const startIso = start.toISOString()
    const endIso = end.toISOString()
    setBusy(true)
    setActionError(null)
    setConflict(null)
    // Optimistic: render the slot as taken immediately, before the
    // network round trip completes (Phase 24 Scope IN: "render as
    // pending immediately, finalize on 201, and on 409 roll back").
    setPendingBlock({ start: startIso, end: endIso, pending: true })
    try {
      const booking = await createBooking({ resource_id: resourceId, start: startIso, end: endIso })
      setPendingBlock(null)
      setJustCreated((current) => [...current, bookingToDisplayBlock(booking)])
      setSelection(null)
    } catch (caught) {
      setPendingBlock(null)
      if (caught instanceof ApiError && caught.code === 'slot_unavailable') {
        // Rollout v1.0 §7: a 409 slot_unavailable here means the system
        // worked exactly as designed — the exclusion constraint did its
        // job and stopped a genuine double-book. This must NEVER be
        // reported to error tracking / paged to on-call the way an
        // actual application failure would be; it is an expected,
        // routine outcome of concurrent booking, not a bug. If/when this
        // frontend adds error-tracking instrumentation, this branch is
        // the one to explicitly exclude from it.
        const nearest = findNearestOpenSlots(
          startIso,
          endIso,
          currentBusyBlocksForConflictSearch(startIso, endIso),
        )
        setConflict({ message: slotUnavailableMessage(nearest), nearestSlots: nearest })
        refetch()
      } else if (caught instanceof ApiError) {
        setActionError(caught.message)
      } else {
        setActionError('Something went wrong. Please try again.')
      }
    } finally {
      setBusy(false)
    }
  }

  async function handleCancel(bookingId: string, reason: string | undefined) {
    setBusy(true)
    setActionError(null)
    try {
      await cancelBooking(bookingId, reason)
      setSelection(null)
      refetch()
    } catch (caught) {
      setActionError(caught instanceof ApiError ? caught.message : 'Could not cancel this booking.')
    } finally {
      setBusy(false)
    }
  }

  async function handleEdit(bookingId: string, start: Date, end: Date) {
    const startIso = start.toISOString()
    const endIso = end.toISOString()
    setBusy(true)
    setActionError(null)
    setConflict(null)
    try {
      await editBooking(bookingId, { start: startIso, end: endIso })
      setSelection(null)
      refetch()
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'slot_unavailable') {
        // Same Rollout v1.0 §7 exclusion applies here as in handleCreate.
        const nearest = findNearestOpenSlots(
          startIso,
          endIso,
          currentBusyBlocksForConflictSearch(startIso, endIso),
        )
        setConflict({ message: slotUnavailableMessage(nearest), nearestSlots: nearest })
        refetch()
      } else if (caught instanceof ApiError) {
        setActionError(caught.message)
      } else {
        setActionError('Could not save this change.')
      }
    } finally {
      setBusy(false)
    }
  }

  function handlePickSuggestedSlot(_slot: OpenSlotOption) {
    setConflict(null)
  }

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <select
          value={resourceId ?? ''}
          onChange={(event) => {
            setSelectedResourceId(event.target.value || null)
          }}
          className="rounded-md border border-gray-300 px-2 py-1.5 text-sm"
          aria-label="Resource"
        >
          {resourcesLoading && <option>Loading resources…</option>}
          {!resourcesLoading && resources.length === 0 && <option>No resources available</option>}
          {resources.map((resource) => (
            <option key={resource.id} value={resource.id}>
              {resource.name}
            </option>
          ))}
        </select>

        <div className="flex gap-1">
          {VIEWS.map((candidate) => (
            <button
              key={candidate}
              type="button"
              onClick={() => {
                setView(candidate)
              }}
              className={`rounded-md px-3 py-1.5 text-sm capitalize ${
                view === candidate
                  ? 'bg-kairos-primary text-white'
                  : 'border border-gray-300 text-gray-700 hover:bg-gray-50'
              }`}
            >
              {candidate}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => {
              setAnchor((current) => stepAnchor(view, current, -1))
            }}
            className="rounded-md border border-gray-300 px-2 py-1.5 text-sm hover:bg-gray-50"
            aria-label="Previous"
          >
            ‹
          </button>
          <button
            type="button"
            onClick={() => {
              setAnchor(new Date())
            }}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm hover:bg-gray-50"
          >
            Today
          </button>
          <button
            type="button"
            onClick={() => {
              setAnchor((current) => stepAnchor(view, current, 1))
            }}
            className="rounded-md border border-gray-300 px-2 py-1.5 text-sm hover:bg-gray-50"
            aria-label="Next"
          >
            ›
          </button>
        </div>

        <span className="ml-auto text-sm text-gray-500">
          {formatDateLabel(from.toISOString(), timeZone)} –{' '}
          {formatDateLabel(new Date(to.getTime() - 1).toISOString(), timeZone)} ·{' '}
          <span data-testid="viewer-timezone">
            {timeZone} ({zoneShortLabel(timeZone)})
          </span>
        </span>
      </div>

      {availabilityLoading && <p className="text-sm text-gray-500">Loading availability…</p>}
      {error !== null && (
        <p role="alert" className="text-sm text-kairos-danger">
          Could not load availability. Try again.
        </p>
      )}

      <div className="flex items-start gap-4">
        <div className="min-w-0 flex-1">
          {view === 'month' ? (
            <MonthGrid
              days={days}
              monthAnchor={anchor}
              blocks={displayBlocks}
              onDayClick={(day) => {
                setAnchor(day)
                setView('day')
              }}
            />
          ) : (
            <TimeGrid
              days={days}
              blocks={displayBlocks}
              timeZone={timeZone}
              onSlotClick={handleSlotClick}
              onBlockClick={handleBlockClick}
              onBusyBlockClick={handleBusyBlockClick}
            />
          )}
        </div>

        {waitlistSelection && resourceId && (
          <JoinWaitlistPanel
            resourceId={resourceId}
            start={waitlistSelection.start}
            end={waitlistSelection.end}
            timeZone={timeZone}
            onClose={() => {
              setWaitlistSelection(null)
            }}
            onBookInstead={handleBookInsteadFromWaitlist}
          />
        )}

        {selection && (
          <BookingPanel
            selection={selection}
            timeZone={timeZone}
            onClose={() => {
              setSelection(null)
              setConflict(null)
              setActionError(null)
            }}
            onCreate={(start, end) => {
              void handleCreate(start, end)
            }}
            onCancel={(bookingId, reason) => {
              void handleCancel(bookingId, reason)
            }}
            onEdit={(bookingId, start, end) => {
              void handleEdit(bookingId, start, end)
            }}
            onPickSuggestedSlot={handlePickSuggestedSlot}
            conflict={conflict}
            actionError={actionError}
            busy={busy}
          />
        )}
      </div>
    </div>
  )
}
