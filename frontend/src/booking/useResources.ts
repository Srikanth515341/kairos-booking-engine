import { useEffect, useState } from 'react'
import { listResources } from '../api/resources'
import type { Resource } from '../api/types'

interface UseResourcesResult {
  resources: Resource[]
  loading: boolean
  error: unknown
}

/** Fetches active resources for the calendar's resource picker. Resource
 * catalogues in this project are small (an operational tool, not a
 * public marketplace), so a single page at the server's max page size is
 * a deliberate, documented simplification rather than building full
 * cursor-pagination into a `<select>` — My Bookings (which genuinely can
 * grow large per-user) is where real cursor pagination lives. */
export function useResources(): UseResourcesResult {
  const [resources, setResources] = useState<Resource[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    let cancelled = false
    listResources({ limit: 100 })
      .then((page) => {
        if (!cancelled) {
          setResources(page.data.filter((resource) => resource.status === 'active'))
          setLoading(false)
        }
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(caught)
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  return { resources, loading, error }
}
