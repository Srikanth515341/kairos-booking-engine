import { useAuth } from '../auth/useAuth'

/** Placeholder landing page — booking/resource/calendar UI is out of
 * scope for Phase 23 ("Frontend Foundation"): this phase proves auth,
 * routing, the API client, and the layout shell work end to end. */
export function DashboardPage() {
  const { user } = useAuth()

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Dashboard</h1>
      <p className="mt-2 text-gray-600">
        Signed in as <span className="font-medium">{user?.email ?? user?.sub}</span>.
      </p>
      <p className="mt-4 text-sm text-gray-500">
        Booking and resource screens land in a later phase — this page exists to prove the
        authenticated app shell (routing, layout, session) works end to end.
      </p>
    </div>
  )
}
