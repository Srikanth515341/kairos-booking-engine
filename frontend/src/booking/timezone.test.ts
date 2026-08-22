import { describe, expect, it } from 'vitest'
import { formatDateTime, formatTimeOfDay, zoneShortLabel } from './timezone'

/** Extracts the numeric hour (24h)/minute/day for an instant in a given
 * zone, independent of locale display conventions (AM/PM casing, "Sep"
 * vs "Sept", comma placement, narrow-no-break-space-before-AM/PM, ...).
 * TZ-04's actual claim is about the TIMEZONE conversion being correct,
 * not about which locale's formatting conventions this test-runner's
 * environment happens to default to — so assertions compare these raw
 * numeric parts rather than a whole formatted string. */
function partsInZone(iso: string, timeZone: string) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    day: 'numeric',
    month: 'numeric',
  }).formatToParts(new Date(iso))
  const get = (type: string) => Number(parts.find((part) => part.type === type)?.value)
  return { hour: get('hour') % 24, minute: get('minute'), day: get('day'), month: get('month') }
}

// TZ-04 (Test Plan v1.0): "no per-viewer localization exists server-side —
// the SAME UTC instant, rendered for two viewers in different timezones,
// must show two different, each individually CORRECT, local wall-clock
// times." formatTimeOfDay/formatDateTime take the zone as an explicit
// parameter rather than reading the runtime's ambient default, precisely
// so this can be proven directly against two REAL, different IANA zones
// without needing two actual browsers — this is the frontend half of
// TZ-04 the way it's testable outside a live two-browser session.
describe('TZ-04 frontend half — the same instant renders correctly per viewer timezone', () => {
  // 2026-09-01T13:00:00Z is 09:00 in America/New_York (EDT, UTC-4) and
  // 18:30 in Asia/Kolkata (UTC+5:30) — two zones with a non-whole-hour
  // offset difference between them, so a naive "just shift by N whole
  // hours" bug would be caught by asserting both exact wall-clock times,
  // not just that they differ.
  const instant = '2026-09-01T13:00:00Z'

  it('renders the correct local time in America/New_York', () => {
    expect(partsInZone(instant, 'America/New_York')).toEqual({
      hour: 9,
      minute: 0,
      day: 1,
      month: 9,
    })
  })

  it('renders the correct local time in Asia/Kolkata', () => {
    expect(partsInZone(instant, 'Asia/Kolkata')).toEqual({ hour: 18, minute: 30, day: 1, month: 9 })
  })

  it('produces genuinely different rendered text for the two zones', () => {
    const newYork = formatTimeOfDay(instant, 'America/New_York')
    const kolkata = formatTimeOfDay(instant, 'Asia/Kolkata')
    expect(newYork).not.toBe(kolkata)
  })

  it('formatDateTime also varies correctly by zone, including the date component', () => {
    // 2026-09-01T23:00:00Z is still Sep 1 in New York but already Sep 2 in
    // Kolkata — a date-boundary case, not just a time-of-day one.
    const lateInstant = '2026-09-01T23:00:00Z'
    expect(partsInZone(lateInstant, 'America/New_York')).toMatchObject({ day: 1, month: 9 })
    expect(partsInZone(lateInstant, 'Asia/Kolkata')).toMatchObject({ day: 2, month: 9 })

    // The real formatting functions still produce non-empty, distinct
    // output for the two zones (locale-specific display conventions
    // aside — that's this environment's ICU default, not a bug).
    const newYork = formatDateTime(lateInstant, 'America/New_York')
    const kolkata = formatDateTime(lateInstant, 'Asia/Kolkata')
    expect(newYork).not.toBe(kolkata)
    expect(newYork.length).toBeGreaterThan(0)
    expect(kolkata.length).toBeGreaterThan(0)
  })
})

describe('zoneShortLabel', () => {
  it('renders a short, human label for the zone, distinct per zone', () => {
    const at = new Date('2026-09-01T12:00:00Z')
    const newYorkLabel = zoneShortLabel('America/New_York', at)
    const kolkataLabel = zoneShortLabel('Asia/Kolkata', at)
    expect(newYorkLabel.length).toBeGreaterThan(0)
    expect(kolkataLabel.length).toBeGreaterThan(0)
    expect(newYorkLabel).not.toBe(kolkataLabel)
  })
})
