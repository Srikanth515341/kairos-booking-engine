import { useState } from 'react'
import { useResources } from '../booking/useResources'
import { getViewerTimeZone } from '../booking/timezone'
import type { RecurringPreviewRequest } from '../api/types'
import { WEEKDAY_LABELS } from './weekday'

function todayIsoDate(): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${String(now.getFullYear())}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

interface SeriesFormProps {
  onPreview: (input: RecurringPreviewRequest) => void
  busy: boolean
  error: string | null
}

export function SeriesForm({ onPreview, busy, error }: SeriesFormProps) {
  const { resources, loading: resourcesLoading } = useResources()
  const [resourceId, setResourceId] = useState('')
  const [timezone, setTimezone] = useState(getViewerTimeZone())
  const [localStartTime, setLocalStartTime] = useState('09:00')
  const [localEndTime, setLocalEndTime] = useState('09:30')
  const [weekday, setWeekday] = useState(new Date().getDay())
  const [seriesStartDate, setSeriesStartDate] = useState(todayIsoDate())
  const [occurrenceCount, setOccurrenceCount] = useState(4)

  const effectiveResourceId = resourceId || (resources[0]?.id ?? '')

  return (
    <form
      className="max-w-lg space-y-4"
      onSubmit={(event) => {
        event.preventDefault()
        if (!effectiveResourceId) return
        onPreview({
          resource_id: effectiveResourceId,
          timezone,
          local_start_time: `${localStartTime}:00`,
          local_end_time: `${localEndTime}:00`,
          weekday,
          series_start_date: seriesStartDate,
          occurrence_count: occurrenceCount,
        })
      }}
    >
      <h1 className="text-2xl font-semibold text-gray-900">New recurring booking</h1>

      <label className="block text-sm font-medium text-gray-700">
        Resource
        <select
          value={effectiveResourceId}
          onChange={(event) => {
            setResourceId(event.target.value)
          }}
          className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
          required
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
        Timezone (IANA identifier)
        <input
          type="text"
          value={timezone}
          onChange={(event) => {
            setTimezone(event.target.value)
          }}
          className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
          required
        />
      </label>

      <div className="flex gap-4">
        <label className="block flex-1 text-sm font-medium text-gray-700">
          Start time
          <input
            type="time"
            value={localStartTime}
            onChange={(event) => {
              setLocalStartTime(event.target.value)
            }}
            className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
            required
          />
        </label>
        <label className="block flex-1 text-sm font-medium text-gray-700">
          End time
          <input
            type="time"
            value={localEndTime}
            onChange={(event) => {
              setLocalEndTime(event.target.value)
            }}
            className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
            required
          />
        </label>
      </div>

      <label className="block text-sm font-medium text-gray-700">
        Weekday
        <select
          value={weekday}
          onChange={(event) => {
            setWeekday(Number(event.target.value))
          }}
          className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
        >
          {WEEKDAY_LABELS.map((label, index) => (
            <option key={label} value={index}>
              {label}
            </option>
          ))}
        </select>
      </label>

      <div className="flex gap-4">
        <label className="block flex-1 text-sm font-medium text-gray-700">
          First occurrence date
          <input
            type="date"
            value={seriesStartDate}
            onChange={(event) => {
              setSeriesStartDate(event.target.value)
            }}
            className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
            required
          />
        </label>
        <label className="block flex-1 text-sm font-medium text-gray-700">
          Number of occurrences
          <input
            type="number"
            min={1}
            max={100}
            value={occurrenceCount}
            onChange={(event) => {
              setOccurrenceCount(Number(event.target.value))
            }}
            className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
            required
          />
        </label>
      </div>

      {error && (
        <p role="alert" className="text-sm text-kairos-danger">
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={busy || !effectiveResourceId}
        className="rounded-md bg-kairos-primary px-4 py-2 text-sm font-medium text-white
          hover:bg-kairos-primary-dark disabled:opacity-50"
      >
        {busy ? 'Loading preview…' : 'Preview series'}
      </button>
    </form>
  )
}
