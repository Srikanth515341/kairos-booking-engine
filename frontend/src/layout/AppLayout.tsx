import { Outlet } from 'react-router-dom'
import { NavBar } from './NavBar'

export function AppLayout() {
  return (
    <div className="min-h-screen bg-gray-50">
      <NavBar />
      <main className="mx-auto max-w-5xl px-4 py-8">
        <Outlet />
      </main>
    </div>
  )
}
