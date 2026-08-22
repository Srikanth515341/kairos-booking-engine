import { halfHourSlotsForDay, isSameDay } from './dateGrid'
import type { DisplayBlock } from './DisplayBlock'
import { formatDateLabel, formatTimeOfDay } from './timezone'

interface TimeGridProps {
  days: Date[]
  blocks: DisplayBlock[]
  timeZone: string
  onSlotClick: (start: Date, end: Date) => void
  onBlockClick: (block: DisplayBlock) => void
  /** Fired for an OPAQUE busy cell — another user's confirmed booking OR
   * a held slot, structurally indistinguishable (see `cellClasses`
   * below) — offering "join the waitlist for this time" (Phase 26 Scope
   * IN: "Join waitlist from a full slot"). */
  onBusyBlockClick: (start: Date, end: Date) => void
}

function blockCoveringSlot(
  blocks: DisplayBlock[],
  slotStart: Date,
  slotEnd: Date,
): DisplayBlock | undefined {
  const startMs = slotStart.getTime()
  const endMs = slotEnd.getTime()
  return blocks.find((block) => {
    const blockStart = Date.parse(block.start)
    const blockEnd = Date.parse(block.end)
    return startMs < blockEnd && blockStart < endMs
  })
}

function cellClasses(block: DisplayBlock | undefined): string {
  if (!block) {
    return 'cursor-pointer bg-white hover:bg-blue-50'
  }
  if (block.pending) {
    return 'cursor-wait animate-pulse bg-kairos-primary/40'
  }
  if (block.booking_id) {
    return 'cursor-pointer bg-kairos-primary text-white hover:bg-kairos-primary-dark'
  }
  // Opaque: another user's confirmed booking OR a held slot — this cell
  // renders IDENTICALLY either way, on purpose (Phase 24 Scope IN: "Held
  // slots render as ordinary busy blocks with no indication they are
  // held"). There is no field on `block` that could tell them apart even
  // if this code wanted to. Still clickable (Phase 26) — to join the
  // waitlist, never to reveal anything about who or what occupies it.
  return 'cursor-pointer bg-gray-300 hover:bg-gray-400'
}

export function TimeGrid({
  days,
  blocks,
  timeZone,
  onSlotClick,
  onBlockClick,
  onBusyBlockClick,
}: TimeGridProps) {
  const slotsForFirstDay = halfHourSlotsForDay(days[0] ?? new Date())

  return (
    <div className="overflow-x-auto">
      <div
        className="grid min-w-max"
        style={{ gridTemplateColumns: `5rem repeat(${String(days.length)}, minmax(7rem, 1fr))` }}
      >
        <div className="sticky top-0 z-10 bg-gray-50" />
        {days.map((day) => (
          <div
            key={day.toISOString()}
            className="sticky top-0 z-10 border-b border-gray-200 bg-gray-50 px-2 py-2
              text-center text-sm font-medium text-gray-700"
          >
            {formatDateLabel(day.toISOString(), timeZone)}
          </div>
        ))}

        {slotsForFirstDay.map((referenceSlot, rowIndex) => (
          <div key={rowIndex} className="contents">
            <div className="border-b border-gray-100 px-2 py-1 text-right text-xs text-gray-400">
              {rowIndex % 2 === 0 ? formatTimeOfDay(referenceSlot.toISOString(), timeZone) : ''}
            </div>
            {days.map((day) => {
              const slotStart = new Date(day)
              slotStart.setHours(referenceSlot.getHours(), referenceSlot.getMinutes(), 0, 0)
              const slotEnd = new Date(slotStart.getTime() + 30 * 60 * 1000)
              const covering = blockCoveringSlot(blocks, slotStart, slotEnd)
              const isPastToday = isSameDay(day, new Date()) && slotEnd < new Date()
              const isManageable = covering?.booking_id !== undefined
              const isBookable = covering === undefined && !isPastToday
              const isOpaqueBusy = covering !== undefined && !covering.pending && !isManageable
              const isClickable = isManageable || isBookable || isOpaqueBusy

              return (
                <button
                  key={`${day.toISOString()}-${String(rowIndex)}`}
                  type="button"
                  disabled={!isClickable}
                  onClick={() => {
                    if (isManageable) {
                      onBlockClick(covering)
                    } else if (isBookable) {
                      onSlotClick(slotStart, slotEnd)
                    } else if (isOpaqueBusy) {
                      onBusyBlockClick(slotStart, slotEnd)
                    }
                  }}
                  className={`h-6 border-b border-gray-100 text-[10px] leading-6 ${cellClasses(
                    covering,
                  )} ${isPastToday && !covering ? 'cursor-not-allowed bg-gray-50' : ''}`}
                  aria-label={
                    isManageable
                      ? `Manage booking ${formatTimeOfDay(slotStart.toISOString(), timeZone)}`
                      : isOpaqueBusy
                        ? `Join waitlist for ${formatTimeOfDay(slotStart.toISOString(), timeZone)}`
                        : covering
                          ? `Busy ${formatTimeOfDay(slotStart.toISOString(), timeZone)}`
                          : `Book ${formatTimeOfDay(slotStart.toISOString(), timeZone)}`
                  }
                />
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}
