import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { getResourceUtilization } from '../api/admin'
import { ApiError } from '../api/errors'
import type { ResourceUtilization } from '../api/types'
import { useResources } from '../booking/useResources'

function isoDateDaysAgo(days: number): string {
  const date = new Date()
  date.setDate(date.getDate() - days)
  return date.toISOString().slice(0, 10)
}

export function UtilizationPage() {
  const { resources, loading: resourcesLoading } = useResources()
  const [searchParams, setSearchParams] = useSearchParams()

  const [resourceId, setResourceId] = useState(searchParams.get('resourceId') ?? '')
  const [from, setFrom] = useState(isoDateDaysAgo(30))
  const [to, setTo] = useState(isoDateDaysAgo(0))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ResourceUtilization | null>(null)

  const effectiveResourceId = resourceId || (resources[0]?.id ?? '')

  async function handleFetch() {
    if (!effectiveResourceId) return
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      const utilization = await getResourceUtilization(effectiveResourceId, from, to)
      setResult(utilization)
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'permission_denied') {
        setError(
          'You need to administer this resource, or have operations/system administrator access, to view its utilization.',
        )
      } else if (caught instanceof ApiError) {
        setError(caught.message)
      } else {
        setError('Could not load utilization.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Resource Utilization</h1>

      <form
        className="mt-4 flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault()
          void handleFetch()
        }}
      >
        <label className="block text-sm font-medium text-gray-700">
          Resource
          <select
            value={effectiveResourceId}
            onChange={(event) => {
              setResourceId(event.target.value)
              setSearchParams({ resourceId: event.target.value })
            }}
            className="mt-1 rounded-md border border-gray-300 px-2 py-1.5 text-sm"
          >
            {resourcesLoading && <option>Loading resources…</option>}
            {resources.map((resource) => (
              <option key={resource.id} value={resource.id}>
                {resource.name}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-sm font-medium text-gray-700">
          From
          <input
            type="date"
            value={from}
            onChange={(event) => {
              setFrom(event.target.value)
            }}
            className="mt-1 rounded-md border border-gray-300 px-2 py-1.5 text-sm"
          />
        </label>
        <label className="block text-sm font-medium text-gray-700">
          To
          <input
            type="date"
            value={to}
            onChange={(event) => {
              setTo(event.target.value)
            }}
            className="mt-1 rounded-md border border-gray-300 px-2 py-1.5 text-sm"
          />
        </label>
        <button
          type="submit"
          disabled={busy || !effectiveResourceId}
          className="rounded-md bg-kairos-primary px-4 py-2 text-sm font-medium text-white
            hover:bg-kairos-primary-dark disabled:opacity-50"
        >
          {busy ? 'Loading…' : 'View utilization'}
        </button>
      </form>

      {error && (
        <p role="alert" className="mt-4 text-sm text-kairos-danger">
          {error}
        </p>
      )}

      {result && (
        <dl className="mt-6 grid max-w-2xl grid-cols-2 gap-4 sm:grid-cols-3">
          {(
            [
              ['Total bookings', result.total_bookings],
              ['Total booked minutes', result.total_booked_minutes],
              ['Waitlist joins', result.waitlist_joins],
              ['Cancellations', result.cancellation_count],
              ['Offers confirmed', result.offers_confirmed],
              ['Offers expired', result.offers_expired],
            ] as const
          ).map(([label, value]) => (
            <div key={label} className="rounded-md border border-gray-200 bg-white p-4">
              <dt className="text-xs text-gray-500">{label}</dt>
              <dd className="mt-1 text-2xl font-semibold text-gray-900">{value}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  )
}
