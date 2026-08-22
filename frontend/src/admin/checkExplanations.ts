import type { CheckName } from '../api/types'

export const CHECK_LABELS: Record<CheckName, string> = {
  schema_assertion: 'Schema assertion',
  reconciliation: 'Reconciliation',
  hold_reaper: 'Hold reaper',
  offer_cascade: 'Offer cascade',
  series_materialization: 'Series materialization',
  tzdata_rematerialization: 'Timezone data re-materialization',
}

/**
 * Shown for EVERY check, always — not only on failure — so an operator
 * knows what a row means before an incident forces them to read it under
 * pressure. `reconciliation`'s own text is this project's own verbatim
 * RUNBOOK-01 framing (kairos/core/reconciliation.py's
 * RECONCILIATION_FAILURE_MESSAGE, reproduced in spirit here — the exact
 * backend string, when a real failure has one, is rendered separately
 * and verbatim from `findings.message`, never paraphrased): a hit here
 * means the correctness guarantee has been REMOVED, not that a race
 * occurred — treating it as timing noise is the wrong response.
 */
export const CHECK_EXPLANATIONS: Record<CheckName, string> = {
  schema_assertion:
    'Confirms the no_overlapping_bookings exclusion constraint still exists and still covers both confirmed and held bookings. A failure means the constraint was narrowed or removed — the core guarantee could now be bypassed.',
  reconciliation:
    'Scans for bookings that overlap on the same resource — something the exclusion constraint should make structurally impossible. This is NOT a race detector: if the constraint is present and functioning, this query cannot return a row. A failure means the guarantee has been REMOVED — a dropped constraint, a restore taken without it, or an out-of-band write — not that a race occurred. Investigating this as a timing issue is the wrong response.',
  hold_reaper:
    'Confirms the background job that reclaims expired holds is still running on schedule. A stale heartbeat does not mean bookings are unsafe (cleanup-on-write is a second, independent mechanism) — it means expired holds may sit unreclaimed until the next real booking attempt on that exact range.',
  offer_cascade:
    'Confirms waitlist offers are not stuck past their expiry with neither reclamation mechanism having reached them. A failure here means someone may be waiting on an offer that should have already moved on to the next person in line.',
  series_materialization:
    'Confirms the rolling job that keeps recurring series materialized into real bookings is still running on schedule.',
  tzdata_rematerialization:
    'Confirms recurring series are re-checked against the installed IANA timezone data after it updates, so a DST rule change gets reflected in already-created future occurrences.',
}
