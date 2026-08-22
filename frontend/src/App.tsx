import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AdminHomePage } from './admin/AdminHomePage'
import { OffboardingPage } from './admin/OffboardingPage'
import { OpsChecksPage } from './admin/OpsChecksPage'
import { ResourceManagementPage } from './admin/ResourceManagementPage'
import { UtilizationPage } from './admin/UtilizationPage'
import { AuthProvider } from './auth/AuthProvider'
import { LoginPage } from './auth/LoginPage'
import { OidcCallbackPage } from './auth/OidcCallbackPage'
import { ProtectedRoute } from './auth/ProtectedRoute'
import { BookingHistoryPage } from './booking/BookingHistoryPage'
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
              <Route path="/bookings/:id/history" element={<BookingHistoryPage />} />
              <Route path="/admin" element={<AdminHomePage />} />
              <Route path="/admin/resources" element={<ResourceManagementPage />} />
              <Route path="/admin/checks" element={<OpsChecksPage />} />
              <Route path="/admin/utilization" element={<UtilizationPage />} />
              <Route path="/admin/offboarding" element={<OffboardingPage />} />
            </Route>
          </Route>

          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}
