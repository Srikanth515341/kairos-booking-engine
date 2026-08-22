import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

const LINKS = [
  {
    to: '/admin/resources',
    label: 'Resource management',
    description: 'Create, edit, take offline, manage admin scope.',
  },
  {
    to: '/admin/checks',
    label: 'Correctness checks',
    description: 'All six background checks, with pass/fail explained inline.',
  },
  {
    to: '/admin/utilization',
    label: 'Utilization',
    description: 'Bookings, booked minutes, waitlist and offer activity per resource.',
  },
  {
    to: '/admin/offboarding',
    label: 'User offboarding',
    description: 'Deactivate a user and see the per-policy cascade outcome.',
  },
]

export function AdminHomePage() {
  const navigate = useNavigate()
  const [lookupId, setLookupId] = useState('')

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Admin &amp; Operations</h1>
      <p className="mt-1 max-w-2xl text-sm text-gray-500">
        These pages are visible to any signed-in user — the backend has no endpoint that tells the
        frontend a user&rsquo;s role in advance, so each page shows its controls to everyone and
        lets the server&rsquo;s own permission check be the real gate. A permission-denied message
        here means exactly that, not a bug.
      </p>

      <ul className="mt-6 grid gap-3 sm:grid-cols-2">
        {LINKS.map((link) => (
          <li key={link.to}>
            <Link
              to={link.to}
              className="block rounded-md border border-gray-200 bg-white p-4 hover:border-kairos-primary"
            >
              <p className="text-sm font-semibold text-gray-900">{link.label}</p>
              <p className="mt-1 text-sm text-gray-500">{link.description}</p>
            </Link>
          </li>
        ))}
      </ul>

      <div className="mt-8 max-w-md rounded-md border border-gray-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-gray-900">Look up a booking&rsquo;s history</h2>
        <p className="mt-1 text-sm text-gray-500">
          Owner, resource admin, or operations only — anyone else gets the same not-found as a
          booking that doesn&rsquo;t exist.
        </p>
        <form
          className="mt-3 flex gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (lookupId.trim()) {
              void navigate(`/bookings/${lookupId.trim()}/history`)
            }
          }}
        >
          <input
            type="text"
            value={lookupId}
            onChange={(event) => {
              setLookupId(event.target.value)
            }}
            placeholder="booking UUID"
            className="flex-1 rounded-md border border-gray-300 px-2 py-1.5 text-sm"
          />
          <button
            type="submit"
            className="rounded-md bg-kairos-primary px-3 py-1.5 text-sm text-white
              hover:bg-kairos-primary-dark"
          >
            View history
          </button>
        </form>
      </div>
    </div>
  )
}
