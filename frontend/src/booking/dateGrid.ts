/**
 * Civil-calendar arithmetic in the VIEWER'S LOCAL timezone. Plain `Date`
 * getters/setters (`getFullYear`, `setDate`, `setHours`, ...) already
 * operate in the browser's own local zone by definition — no timezone
 * library is needed for "what is the start of today/this week/this
 * month" the way one would be needed to convert between two DIFFERENT
 * zones. `.toISOString()` on the resulting boundary is what turns a
 * local-time boundary into the UTC instant the backend's `from`/`to`
 * query params expect.
 */

export type CalendarView = 'day' | 'week' | 'month'

export function startOfDay(date: Date): Date {
  const result = new Date(date)
  result.setHours(0, 0, 0, 0)
  return result
}

export function addDays(date: Date, days: number): Date {
  const result = new Date(date)
  result.setDate(result.getDate() + days)
  return result
}

export function addMonths(date: Date, months: number): Date {
  const result = new Date(date)
  result.setMonth(result.getMonth() + months)
  return result
}

/** Monday-based week start (ISO-ish; the exact anchor day doesn't matter
 * for correctness, only consistency between rendering and querying). */
export function startOfWeek(date: Date): Date {
  const day = date.getDay() // 0 = Sunday
  const diffToMonday = day === 0 ? -6 : 1 - day
  return startOfDay(addDays(date, diffToMonday))
}

export function startOfMonth(date: Date): Date {
  const result = startOfDay(date)
  result.setDate(1)
  return result
}

export function isSameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  )
}

/** The half-open `[from, to)` LOCAL-time boundary this view needs data
 * for. Callers convert both ends to UTC ISO (`.toISOString()`) before
 * calling the availability API. */
export function rangeForView(view: CalendarView, anchor: Date): { from: Date; to: Date } {
  switch (view) {
    case 'day': {
      const from = startOfDay(anchor)
      return { from, to: addDays(from, 1) }
    }
    case 'week': {
      const from = startOfWeek(anchor)
      return { from, to: addDays(from, 7) }
    }
    case 'month': {
      const from = startOfWeek(startOfMonth(anchor))
      const monthEnd = addMonths(startOfMonth(anchor), 1)
      // Extend to a full week grid (six rows is enough for any month).
      const to = startOfWeek(addDays(monthEnd, 6))
      return { from, to }
    }
  }
}

/** Every LOCAL day covered by a view, in order — used to render one
 * column per day (day/week) or one cell per day (month). */
export function daysInView(view: CalendarView, anchor: Date): Date[] {
  const { from, to } = rangeForView(view, anchor)
  const days: Date[] = []
  let cursor = from
  while (cursor < to) {
    days.push(cursor)
    cursor = addDays(cursor, 1)
  }
  return days
}

/** Half-hour slot start times covering a single local day — the grid
 * granularity for day/week views. */
export function halfHourSlotsForDay(day: Date): Date[] {
  const start = startOfDay(day)
  const slots: Date[] = []
  for (let minutes = 0; minutes < 24 * 60; minutes += 30) {
    const slot = new Date(start)
    slot.setMinutes(minutes)
    slots.push(slot)
  }
  return slots
}

/** `<input type="datetime-local">` values are already local-wall-clock
 * strings with no offset — this is a plain, lossless string format, not
 * a timezone conversion. */
export function toDatetimeLocalValue(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${String(date.getFullYear())}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

export function fromDatetimeLocalValue(value: string): Date {
  return new Date(value)
}

export function stepAnchor(view: CalendarView, anchor: Date, direction: 1 | -1): Date {
  switch (view) {
    case 'day':
      return addDays(anchor, direction)
    case 'week':
      return addDays(anchor, 7 * direction)
    case 'month':
      return addMonths(anchor, direction)
  }
}
