/** Index matches both the backend's `weekday` field (Spec v1.0 §5.8: 0 =
 * Sunday … 6 = Saturday) and JavaScript's own `Date.getDay()` — no
 * conversion needed between the two. */
export const WEEKDAY_LABELS = [
  'Sunday',
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
] as const

/** Strips a "HH:MM:SS" wall-clock string down to "HH:MM" for display —
 * these are local times in the SERIES' own timezone, not instants, so
 * there is nothing to convert, only to trim. */
export function trimSeconds(hhmmss: string): string {
  return hhmmss.slice(0, 5)
}
