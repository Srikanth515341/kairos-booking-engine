import { NavLink } from 'react-router-dom'
import { useAuth } from '../auth/useAuth'

const linkClasses = ({ isActive }: { isActive: boolean }) =>
  `rounded-md px-3 py-2 text-sm font-medium ${
    isActive ? 'bg-kairos-primary text-white' : 'text-gray-600 hover:bg-gray-100'
  }`

export function NavBar() {
  const { user, logout } = useAuth()

  return (
    <header className="border-b border-gray-200 bg-white">
      <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
        <div className="flex items-center gap-6">
          <span className="text-lg font-semibold text-kairos-primary">Kairos</span>
          <nav className="flex gap-1">
            <NavLink to="/" end className={linkClasses}>
              Dashboard
            </NavLink>
            <NavLink to="/calendar" className={linkClasses}>
              Calendar
            </NavLink>
            <NavLink to="/bookings" className={linkClasses}>
              My Bookings
            </NavLink>
            <NavLink to="/recurring" className={linkClasses}>
              Recurring
            </NavLink>
            <NavLink to="/waitlist" className={linkClasses}>
              Waitlist
            </NavLink>
            <NavLink to="/admin" className={linkClasses}>
              Admin
            </NavLink>
          </nav>
        </div>
        <div className="flex items-center gap-3 text-sm text-gray-600">
          <span data-testid="current-user">{user?.email ?? user?.name ?? user?.sub}</span>
          <button
            type="button"
            onClick={logout}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700
              hover:bg-gray-50"
          >
            Sign out
          </button>
        </div>
      </div>
    </header>
  )
}
