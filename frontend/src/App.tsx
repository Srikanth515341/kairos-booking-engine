import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AuthProvider } from './auth/AuthProvider'
import { LoginPage } from './auth/LoginPage'
import { OidcCallbackPage } from './auth/OidcCallbackPage'
import { ProtectedRoute } from './auth/ProtectedRoute'
import { CalendarPage } from './booking/CalendarPage'
import { AppLayout } from './layout/AppLayout'
import { DashboardPage } from './pages/DashboardPage'
import { MyBookingsPage } from './pages/MyBookingsPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { RecurringSeriesPage } from './recurring/RecurringSeriesPage'
import { MyWaitlistPage } from './waitlist/MyWaitlistPage'

export function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/auth/callback" element={<OidcCallbackPage />} />

          <Route element={<ProtectedRoute />}>
            <Route element={<AppLayout />}>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/calendar" element={<CalendarPage />} />
              <Route path="/bookings" element={<MyBookingsPage />} />
              <Route path="/recurring" element={<RecurringSeriesPage />} />
              <Route path="/waitlist" element={<MyWaitlistPage />} />
            </Route>
          </Route>

          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}
