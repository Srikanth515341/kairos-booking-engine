import { useState } from 'react'
import type {
  Resource,
  ResourceCreateInput,
  ResourceOffboardingPolicy,
  ResourceStatus,
  ResourceUpdateInput,
} from '../api/types'

const OFFBOARDING_POLICIES: ResourceOffboardingPolicy[] = [
  'transfer',
  'cancel_and_notify',
  'retain',
]

interface CreateFormProps {
  mode: 'create'
  onSubmit: (input: ResourceCreateInput) => void
  onCancel?: () => void
  busy: boolean
  error: string | null
}

interface EditFormProps {
  mode: 'edit'
  resource: Resource
  onSubmit: (input: ResourceUpdateInput) => void
  onCancel: () => void
  busy: boolean
  error: string | null
}

type ResourceFormProps = CreateFormProps | EditFormProps

/**
 * ONE component for both create and edit, not two — but the field sets
 * are deliberately NOT identical: `PATCH /resources/{id}`'s real
 * updatable-field list (Spec v1.0 §5.14) never includes `category`,
 * `timezone`, or `restricted_group_id` (create-only), and only edit
 * exposes `status` (offline/online) at all. Rendering a field the
 * backend would silently ignore would be worse than not offering it.
 */
export function ResourceForm(props: ResourceFormProps) {
  const initial = props.mode === 'edit' ? props.resource : null

  const [name, setName] = useState(initial?.name ?? '')
  const [category, setCategory] = useState(initial?.category ?? 'meeting_room')
  const [timezone, setTimezone] = useState(
    initial?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone,
  )
  const [bookableStart, setBookableStart] = useState(
    (initial?.bookable_start_time ?? '09:00:00').slice(0, 5),
  )
  const [bookableEnd, setBookableEnd] = useState(
    (initial?.bookable_end_time ?? '17:00:00').slice(0, 5),
  )
  const [maxDuration, setMaxDuration] = useState(
    initial?.max_booking_duration_minutes != null
      ? String(initial.max_booking_duration_minutes)
      : '',
  )
  const [offboardingPolicy, setOffboardingPolicy] = useState<ResourceOffboardingPolicy>(
    initial?.offboarding_policy ?? 'transfer',
  )
  const [status, setStatus] = useState<ResourceStatus>(initial?.status ?? 'active')

  return (
    <form
      className="mt-3 space-y-3 rounded-md border border-gray-200 bg-gray-50 p-4"
      onSubmit={(event) => {
        event.preventDefault()
        const maxDurationValue = maxDuration.trim() === '' ? null : Number(maxDuration)
        if (props.mode === 'create') {
          props.onSubmit({
            name,
            category,
            timezone,
            bookable_start_time: `${bookableStart}:00`,
            bookable_end_time: `${bookableEnd}:00`,
            max_booking_duration_minutes: maxDurationValue,
            offboarding_policy: offboardingPolicy,
          })
        } else {
          props.onSubmit({
            name,
            bookable_start_time: `${bookableStart}:00`,
            bookable_end_time: `${bookableEnd}:00`,
            max_booking_duration_minutes: maxDurationValue,
            offboarding_policy: offboardingPolicy,
            status,
          })
        }
      }}
    >
      <label className="block text-sm font-medium text-gray-700">
        Name
        <input
          type="text"
          value={name}
          onChange={(event) => {
            setName(event.target.value)
          }}
          className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
          required
        />
      </label>

      {props.mode === 'create' && (
        <>
          <label className="block text-sm font-medium text-gray-700">
            Category
            <input
              type="text"
              value={category}
              onChange={(event) => {
                setCategory(event.target.value)
              }}
              className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
            />
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
        </>
      )}

      <div className="flex gap-4">
        <label className="block flex-1 text-sm font-medium text-gray-700">
          Bookable from
          <input
            type="time"
            value={bookableStart}
            onChange={(event) => {
              setBookableStart(event.target.value)
            }}
            className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
            required
          />
        </label>
        <label className="block flex-1 text-sm font-medium text-gray-700">
          Bookable until
          <input
            type="time"
            value={bookableEnd}
            onChange={(event) => {
              setBookableEnd(event.target.value)
            }}
            className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
            required
          />
        </label>
      </div>

      <label className="block text-sm font-medium text-gray-700">
        Max booking duration (minutes, blank = no limit)
        <input
          type="number"
          min={1}
          value={maxDuration}
          onChange={(event) => {
            setMaxDuration(event.target.value)
          }}
          className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
        />
      </label>

      <label className="block text-sm font-medium text-gray-700">
        Offboarding policy
        <select
          value={offboardingPolicy}
          onChange={(event) => {
            setOffboardingPolicy(event.target.value as ResourceOffboardingPolicy)
          }}
          className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
        >
          {OFFBOARDING_POLICIES.map((policy) => (
            <option key={policy} value={policy}>
              {policy}
            </option>
          ))}
        </select>
      </label>

      {props.mode === 'edit' && (
        <label className="block text-sm font-medium text-gray-700">
          Status
          <select
            value={status}
            onChange={(event) => {
              setStatus(event.target.value as ResourceStatus)
            }}
            className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
          >
            <option value="active">active</option>
            <option value="inactive">inactive (offline)</option>
          </select>
        </label>
      )}

      {props.error && (
        <p role="alert" className="text-sm text-kairos-danger">
          {props.error}
        </p>
      )}

      <div className="flex gap-2">
        <button
          type="submit"
          disabled={props.busy}
          className="rounded-md bg-kairos-primary px-4 py-2 text-sm font-medium text-white
            hover:bg-kairos-primary-dark disabled:opacity-50"
        >
          {props.busy ? 'Saving…' : props.mode === 'create' ? 'Create resource' : 'Save changes'}
        </button>
        {props.onCancel && (
          <button
            type="button"
            onClick={props.onCancel}
            className="rounded-md border border-gray-300 px-4 py-2 text-sm text-gray-700
              hover:bg-gray-50"
          >
            Cancel
          </button>
        )}
      </div>
    </form>
  )
}
