/**
 * All conversion here is deliberately a pure function of (instant, IANA
 * zone) — never the runtime's ambient default alone — specifically so it
 * is testable the way TZ-04 asks: the SAME UTC instant must render
 * correctly for a viewer in ANY timezone, not just whichever one this
 * machine happens to be running in. `getViewerTimeZone()` is the only
 * place that reads the ambient default; every formatter takes a zone
 * explicitly.
 */

/** The IANA zone identifier for whoever is looking at this page right
 * now (e.g. "America/New_York") — shown explicitly in the calendar
 * header per Phase 24's own Scope IN, not left implicit. */
export function getViewerTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone
}

export function formatTimeOfDay(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat(undefined, {
    timeZone,
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(iso))
}

export function formatDateLabel(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat(undefined, {
    timeZone,
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  }).format(new Date(iso))
}

export function formatDateTime(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat(undefined, {
    timeZone,
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(iso))
}

/** A short, human label for the zone itself ("EST", "GMT+5:30") — this is
 * the "shown explicitly" part of "rendered in the viewer's local
 * timezone with the timezone shown explicitly" (Phase 24 Scope IN). */
export function zoneShortLabel(timeZone: string, at: Date = new Date()): string {
  const parts = new Intl.DateTimeFormat(undefined, {
    timeZone,
    timeZoneName: 'short',
  }).formatToParts(at)
  return parts.find((part) => part.type === 'timeZoneName')?.value ?? timeZone
}
