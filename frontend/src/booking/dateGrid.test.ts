import { describe, expect, it } from 'vitest'
import {
  addDays,
  daysInView,
  fromDatetimeLocalValue,
  halfHourSlotsForDay,
  isSameDay,
  rangeForView,
  startOfMonth,
  startOfWeek,
  stepAnchor,
  toDatetimeLocalValue,
} from './dateGrid'

describe('startOfWeek', () => {
  it('resolves to the preceding Monday, at local midnight', () => {
    const wednesday = new Date(2026, 8, 2, 14, 30) // Wed Sep 2 2026, 14:30 local
    const monday = startOfWeek(wednesday)
    expect(monday.getDay()).toBe(1)
    expect(monday.getDate()).toBe(31) // Aug 31 2026 is the Monday of that week
    expect(monday.getHours()).toBe(0)
    expect(monday.getMinutes()).toBe(0)
  })

  it('treats Sunday as the END of its week, not the start', () => {
    const sunday = new Date(2026, 8, 6, 9, 0)
    const monday = startOfWeek(sunday)
    expect(monday.getDay()).toBe(1)
    expect(monday.getDate()).toBe(31)
  })
})

describe('rangeForView', () => {
  it('day view covers exactly [midnight, next midnight)', () => {
    const anchor = new Date(2026, 8, 15, 13, 0)
    const { from, to } = rangeForView('day', anchor)
    expect(from.getDate()).toBe(15)
    expect(from.getHours()).toBe(0)
    expect(to.getDate()).toBe(16)
    expect(to.getHours()).toBe(0)
  })

  it('week view covers exactly 7 days', () => {
    const anchor = new Date(2026, 8, 15)
    const { from, to } = rangeForView('week', anchor)
    expect((to.getTime() - from.getTime()) / (1000 * 60 * 60 * 24)).toBe(7)
  })

  it('month view starts on the Monday on/before the 1st and covers the full month', () => {
    const anchor = new Date(2026, 8, 15) // September 2026
    const { from, to } = rangeForView('month', anchor)
    expect(from.getDay()).toBe(1)
    expect(from.getTime()).toBeLessThanOrEqual(startOfMonth(anchor).getTime())
    expect(to.getTime()).toBeGreaterThan(new Date(2026, 8, 30).getTime())
  })
})

describe('daysInView', () => {
  it('returns one Date per day, in order, with no gaps', () => {
    const days = daysInView('week', new Date(2026, 8, 15))
    expect(days).toHaveLength(7)
    for (let i = 1; i < days.length; i++) {
      const previous = days[i - 1]
      const current = days[i]
      expect(previous).toBeDefined()
      expect(current).toBeDefined()
      if (previous && current) {
        expect(addDays(previous, 1).getTime()).toBe(current.getTime())
      }
    }
  })
})

describe('halfHourSlotsForDay', () => {
  it('produces 48 slots covering the full day', () => {
    const slots = halfHourSlotsForDay(new Date(2026, 8, 15, 12, 0))
    expect(slots).toHaveLength(48)
    expect(slots[0]?.getHours()).toBe(0)
    expect(slots[0]?.getMinutes()).toBe(0)
    expect(slots[47]?.getHours()).toBe(23)
    expect(slots[47]?.getMinutes()).toBe(30)
  })
})

describe('stepAnchor', () => {
  it('steps by one day/week/month depending on the view', () => {
    const anchor = new Date(2026, 8, 15)
    expect(isSameDay(stepAnchor('day', anchor, 1), new Date(2026, 8, 16))).toBe(true)
    expect(isSameDay(stepAnchor('week', anchor, 1), new Date(2026, 8, 22))).toBe(true)
    expect(stepAnchor('month', anchor, 1).getMonth()).toBe(9)
  })
})

describe('datetime-local round trip', () => {
  it('converts a Date to a datetime-local value and back losslessly', () => {
    const original = new Date(2026, 8, 15, 9, 30)
    const value = toDatetimeLocalValue(original)
    expect(value).toBe('2026-09-15T09:30')
    const roundTripped = fromDatetimeLocalValue(value)
    expect(roundTripped.getTime()).toBe(original.getTime())
  })
})
