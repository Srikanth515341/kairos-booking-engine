import { describe, expect, it } from 'vitest'
import type { AvailabilityBusyBlock } from '../api/types'
import { findNearestOpenSlots, slotUnavailableMessage } from './conflicts'

describe('findNearestOpenSlots', () => {
  it('finds the closest free slot of the same duration, skipping busy ones', () => {
    const desiredStart = '2026-09-01T10:00:00Z'
    const desiredEnd = '2026-09-01T11:00:00Z'
    const busy: AvailabilityBusyBlock[] = [
      // The desired slot itself, plus the next hour, are both taken —
      // the nearest genuinely free hour-long slot is 12:00-13:00.
      { start: '2026-09-01T10:00:00Z', end: '2026-09-01T11:00:00Z' },
      { start: '2026-09-01T11:00:00Z', end: '2026-09-01T12:00:00Z' },
    ]

    const slots = findNearestOpenSlots(desiredStart, desiredEnd, busy, 3)

    expect(slots.length).toBeGreaterThan(0)
    // The closest genuinely free hour is BEFORE the busy stretch (09:00),
    // one step closer to the desired time than the next free hour after
    // it (12:00) — proving the search checks both directions and picks
    // the truly nearest one, not just "the first free slot after."
    expect(slots[0]).toEqual({ start: '2026-09-01T09:00:00.000Z', end: '2026-09-01T10:00:00.000Z' })
    // None of the returned candidates may overlap a busy block.
    for (const slot of slots) {
      const slotStart = Date.parse(slot.start)
      const slotEnd = Date.parse(slot.end)
      for (const block of busy) {
        const blockStart = Date.parse(block.start)
        const blockEnd = Date.parse(block.end)
        expect(slotStart < blockEnd && blockStart < slotEnd).toBe(false)
      }
    }
  })

  it('returns candidates sorted by proximity to the originally desired time', () => {
    const desiredStart = '2026-09-01T10:00:00Z'
    const desiredEnd = '2026-09-01T10:30:00Z'
    const slots = findNearestOpenSlots(desiredStart, desiredEnd, [], 4)
    const anchor = Date.parse(desiredStart)
    const distances = slots.map((slot) => Math.abs(Date.parse(slot.start) - anchor))
    const sorted = [...distances].sort((a, b) => a - b)
    expect(distances).toEqual(sorted)
  })

  it('returns an empty list for a malformed (zero/negative duration) request', () => {
    expect(findNearestOpenSlots('2026-09-01T10:00:00Z', '2026-09-01T10:00:00Z', [])).toEqual([])
  })
})

describe('slotUnavailableMessage', () => {
  it('is specific and actionable when open slots were found — not "booking failed"', () => {
    const message = slotUnavailableMessage([
      { start: '2026-09-01T12:00:00Z', end: '2026-09-01T13:00:00Z' },
    ])
    expect(message.toLowerCase()).not.toContain('booking failed')
    expect(message).toContain('nearest open slots')
  })

  it('still avoids generic "failed" phrasing even when nothing nearby is free', () => {
    const message = slotUnavailableMessage([])
    expect(message.toLowerCase()).not.toBe('booking failed.')
    expect(message).toContain('Someone booked this a moment ago')
  })
})
