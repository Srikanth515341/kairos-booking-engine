import type { AvailabilityBusyBlock } from '../api/types'

const SEARCH_STEP_MINUTES = 30
const SEARCH_RADIUS_MINUTES = 6 * 60

interface Interval {
  start: number
  end: number
}

function toIntervals(blocks: AvailabilityBusyBlock[]): Interval[] {
  return blocks
    .map((block) => ({ start: Date.parse(block.start), end: Date.parse(block.end) }))
    .sort((a, b) => a.start - b.start)
}

function overlapsAny(candidateStart: number, candidateEnd: number, intervals: Interval[]): boolean {
  return intervals.some(
    (interval) => candidateStart < interval.end && interval.start < candidateEnd,
  )
}

export interface OpenSlot {
  start: string
  end: string
}

/**
 * Scans outward (alternating later/earlier) from the desired start, at
 * SEARCH_STEP_MINUTES granularity, for free slots of the SAME duration
 * as requested — this is what turns "booking failed" into "here are the
 * nearest open slots" (PRD R6: generic conflict messaging is a real
 * product risk, not a nice-to-have).
 */
export function findNearestOpenSlots(
  desiredStart: string,
  desiredEnd: string,
  busyBlocks: AvailabilityBusyBlock[],
  limit = 3,
): OpenSlot[] {
  const durationMs = Date.parse(desiredEnd) - Date.parse(desiredStart)
  if (!(durationMs > 0)) return []

  const intervals = toIntervals(busyBlocks)
  const stepMs = SEARCH_STEP_MINUTES * 60 * 1000
  const radiusMs = SEARCH_RADIUS_MINUTES * 60 * 1000
  const anchor = Date.parse(desiredStart)

  const found: OpenSlot[] = []
  for (let offset = stepMs; offset <= radiusMs && found.length < limit * 2; offset += stepMs) {
    for (const direction of [1, -1] as const) {
      const candidateStart = anchor + direction * offset
      const candidateEnd = candidateStart + durationMs
      if (!overlapsAny(candidateStart, candidateEnd, intervals)) {
        found.push({
          start: new Date(candidateStart).toISOString(),
          end: new Date(candidateEnd).toISOString(),
        })
      }
    }
  }

  return found
    .sort((a, b) => Math.abs(Date.parse(a.start) - anchor) - Math.abs(Date.parse(b.start) - anchor))
    .slice(0, limit)
}

/**
 * Specific, actionable text for a 409 `slot_unavailable` — never the
 * generic "booking failed" PRD R6 flags as a real product risk. The
 * caller renders `nearestSlots` (already found via findNearestOpenSlots
 * against freshly re-fetched availability) as clickable chips alongside
 * this sentence.
 */
export function slotUnavailableMessage(nearestSlots: OpenSlot[]): string {
  if (nearestSlots.length === 0) {
    return 'Someone booked this a moment ago, and no nearby open slots were found today. Try a different day.'
  }
  return 'Someone booked this a moment ago. Here are the nearest open slots.'
}
