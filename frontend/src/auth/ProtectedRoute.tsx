import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from './useAuth'

/** Wraps a `<Route>` subtree — redirects to `/login` (preserving the
 * originally-requested location so login can return there) unless a
 * session is present. `AuthProvider` restores any existing session
 * synchronously (a lazy `useState` initializer reading `sessionStorage`),
 * so there is no intermediate "still checking" render to account for
 * here. */
export function ProtectedRoute() {
  const { status } = useAuth()
  const location = useLocation()

  if (status === 'unauthenticated') {
    return <Navigate to="/login" replace state={{ from: location }} />
  }
  return <Outlet />
}
