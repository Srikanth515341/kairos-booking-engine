import { addDays, isSameDay } from './dateGrid'
import type { DisplayBlock } from './DisplayBlock'

interface MonthGridProps {
  days: Date[]
  monthAnchor: Date
  blocks: DisplayBlock[]
  onDayClick: (day: Date) => void
}

function busyCountForDay(day: Date, blocks: DisplayBlock[]): number {
  const dayStart = day.getTime()
  const dayEnd = addDays(day, 1).getTime()
  return blocks.filter((block) => {
    const blockStart = Date.parse(block.start)
    const blockEnd = Date.parse(block.end)
    return dayStart < blockEnd && blockStart < dayEnd
  }).length
}

const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

export function MonthGrid({ days, monthAnchor, blocks, onDayClick }: MonthGridProps) {
  const today = new Date()

  return (
    <div className="grid grid-cols-7 gap-px overflow-hidden rounded-md border border-gray-200 bg-gray-200">
      {WEEKDAY_LABELS.map((label) => (
        <div
          key={label}
          className="bg-gray-50 px-2 py-1 text-center text-xs font-medium text-gray-500"
        >
          {label}
        </div>
      ))}
      {days.map((day) => {
        const inCurrentMonth = day.getMonth() === monthAnchor.getMonth()
        const count = busyCountForDay(day, blocks)
        return (
          <button
            key={day.toISOString()}
            type="button"
            onClick={() => {
              onDayClick(day)
            }}
            className={`flex min-h-16 flex-col items-start gap-1 bg-white p-2 text-left
              hover:bg-blue-50 ${inCurrentMonth ? '' : 'text-gray-400'} ${
                isSameDay(day, today) ? 'ring-2 ring-kairos-primary ring-inset' : ''
              }`}
          >
            <span className="text-xs font-medium">{day.getDate()}</span>
            {count > 0 && (
              <span className="rounded-full bg-gray-300 px-1.5 py-0.5 text-[10px] text-gray-700">
                {count} busy
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}
