import { Link } from 'react-router-dom'

export function NotFoundPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <div className="text-center">
        <h1 className="text-2xl font-semibold text-gray-900">Page not found</h1>
        <Link to="/" className="mt-2 inline-block text-sm text-kairos-primary hover:underline">
          Back to dashboard
        </Link>
      </div>
    </div>
  )
}
