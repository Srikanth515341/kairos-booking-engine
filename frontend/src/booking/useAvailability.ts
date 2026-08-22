import { useCallback, useEffect, useState } from 'react'
import { getAvailability } from '../api/resources'
import type { AvailabilityResponse } from '../api/types'

interface AvailabilityState {
  /** The (resourceId, from, to, generation) key this data/error actually
   * belongs to — compared against the CURRENT key (computed at render
   * time) to derive `loading`, rather than storing a separate `loading`
   * boolean that would need its own synchronous setState call inside the
   * effect body. */
  key: string
  data: AvailabilityResponse | null
  error: unknown
}

export interface UseAvailabilityResult {
  data: AvailabilityResponse | null
  loading: boolean
  error: unknown
  /** Re-runs the same query — the caller passes this after a 409 so the
   * user sees current state instead of the stale view that led them
   * there (Phase 24 Scope IN's own wording), and after a successful
   * cancel/edit so freed or moved time reappears/disappears promptly. */
  refetch: () => void
}

const EMPTY_STATE: AvailabilityState = { key: '', data: null, error: null }

/** `resourceId` may be null while no resource is selected yet — the hook
 * simply doesn't fetch in that case. `from`/`to` are UTC ISO instants
 * (see booking/dateGrid.ts for converting a local calendar range into
 * them). */
export function useAvailability(
  resourceId: string | null,
  from: string,
  to: string,
): UseAvailabilityResult {
  const [state, setState] = useState<AvailabilityState>(EMPTY_STATE)
  const [generation, setGeneration] = useState(0)

  const refetch = useCallback(() => {
    setGeneration((current) => current + 1)
  }, [])

  const fetchKey = resourceId ? `${resourceId}|${from}|${to}|${String(generation)}` : ''

  useEffect(() => {
    if (!resourceId) {
      return
    }
    let cancelled = false
    getAvailability(resourceId, from, to)
      .then((data) => {
        if (!cancelled) setState({ key: fetchKey, data, error: null })
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ key: fetchKey, data: null, error })
      })
    return () => {
      cancelled = true
    }
  }, [resourceId, from, to, fetchKey])

  if (!resourceId) {
    return { data: null, loading: false, error: null, refetch }
  }

  const loading = state.key !== fetchKey
  return {
    // Keep showing the last known data while a new fetch is in flight
    // (avoids an availability-grid flicker on every refetch/navigation).
    data: state.data,
    loading,
    error: loading ? null : state.error,
    refetch,
  }
}
