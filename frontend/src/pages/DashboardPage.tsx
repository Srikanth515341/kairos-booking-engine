import { Link } from 'react-router-dom'
import { useAuth } from '../auth/useAuth'

export function DashboardPage() {
  const { user } = useAuth()

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Dashboard</h1>
      <p className="mt-2 text-gray-600">
        Signed in as <span className="font-medium">{user?.email ?? user?.sub}</span>.
      </p>
      <div className="mt-6 flex gap-3">
        <Link
          to="/calendar"
          className="rounded-md bg-kairos-primary px-4 py-2 text-sm font-medium text-white
            hover:bg-kairos-primary-dark"
        >
          View calendar
        </Link>
        <Link
          to="/bookings"
          className="rounded-md border border-gray-300 px-4 py-2 text-sm font-medium
            text-gray-700 hover:bg-gray-50"
        >
          My bookings
        </Link>
      </div>
    </div>
  )
}
