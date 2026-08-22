import { useState } from 'react'
import { grantResourceAdmin, revokeResourceAdmin } from '../api/admin'
import { ApiError } from '../api/errors'

interface ResourceAdminsFormProps {
  resourceId: string
  onClose: () => void
}

/**
 * Grant/revoke ONLY — there is no `GET` endpoint anywhere in this API
 * that lists a resource's current admins (confirmed by reading
 * `kairos/resources/urls.py` in full), so this form cannot show who
 * currently administers this resource, only accept a `user_id` to grant
 * or revoke. Matches the exact shape the backend actually exposes,
 * rather than inventing a lookup/search UI no endpoint backs.
 */
export function ResourceAdminsForm({ resourceId, onClose }: ResourceAdminsFormProps) {
  const [grantUserId, setGrantUserId] = useState('')
  const [revokeUserId, setRevokeUserId] = useState('')
  const [grantBusy, setGrantBusy] = useState(false)
  const [revokeBusy, setRevokeBusy] = useState(false)
  const [grantResult, setGrantResult] = useState<string | null>(null)
  const [grantError, setGrantError] = useState<string | null>(null)
  const [revokeResult, setRevokeResult] = useState<string | null>(null)
  const [revokeError, setRevokeError] = useState<string | null>(null)

  async function handleGrant() {
    setGrantBusy(true)
    setGrantError(null)
    setGrantResult(null)
    try {
      const grant = await grantResourceAdmin(resourceId, grantUserId.trim())
      setGrantResult(`Granted — admin since ${grant.granted_at}.`)
      setGrantUserId('')
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'permission_denied') {
        setGrantError(
          'You need to be an administrator of this resource, or a system administrator, to grant this.',
        )
      } else if (caught instanceof ApiError && caught.code === 'already_resource_admin') {
        setGrantError('This user already administers this resource.')
      } else if (caught instanceof ApiError) {
        setGrantError(caught.message)
      } else {
        setGrantError('Could not grant admin access.')
      }
    } finally {
      setGrantBusy(false)
    }
  }

  async function handleRevoke() {
    setRevokeBusy(true)
    setRevokeError(null)
    setRevokeResult(null)
    try {
      await revokeResourceAdmin(resourceId, revokeUserId.trim())
      setRevokeResult('Revoked.')
      setRevokeUserId('')
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'permission_denied') {
        setRevokeError(
          'You need to be an administrator of this resource, or a system administrator, to revoke this.',
        )
      } else if (caught instanceof ApiError && caught.code === 'not_found') {
        setRevokeError('That user does not administer this resource.')
      } else if (caught instanceof ApiError) {
        setRevokeError(caught.message)
      } else {
        setRevokeError('Could not revoke admin access.')
      }
    } finally {
      setRevokeBusy(false)
    }
  }

  return (
    <div className="mt-3 space-y-4 rounded-md border border-gray-200 bg-gray-50 p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">Manage admins</h3>
        <button
          type="button"
          onClick={onClose}
          className="text-sm text-gray-400 hover:text-gray-600"
          aria-label="Close"
        >
          ×
        </button>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700">
          Grant admin — user ID
          <div className="mt-1 flex gap-2">
            <input
              type="text"
              value={grantUserId}
              onChange={(event) => {
                setGrantUserId(event.target.value)
              }}
              placeholder="user UUID"
              className="flex-1 rounded-md border border-gray-300 px-2 py-1.5 text-sm"
            />
            <button
              type="button"
              disabled={grantBusy || !grantUserId.trim()}
              onClick={() => {
                void handleGrant()
              }}
              className="rounded-md bg-kairos-primary px-3 py-1.5 text-sm text-white
                hover:bg-kairos-primary-dark disabled:opacity-50"
            >
              {grantBusy ? 'Granting…' : 'Grant'}
            </button>
          </div>
        </label>
        {grantResult && (
          <p role="status" className="mt-1 text-sm text-green-700">
            {grantResult}
          </p>
        )}
        {grantError && (
          <p role="alert" className="mt-1 text-sm text-kairos-danger">
            {grantError}
          </p>
        )}
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700">
          Revoke admin — user ID
          <div className="mt-1 flex gap-2">
            <input
              type="text"
              value={revokeUserId}
              onChange={(event) => {
                setRevokeUserId(event.target.value)
              }}
              placeholder="user UUID"
              className="flex-1 rounded-md border border-gray-300 px-2 py-1.5 text-sm"
            />
            <button
              type="button"
              disabled={revokeBusy || !revokeUserId.trim()}
              onClick={() => {
                void handleRevoke()
              }}
              className="rounded-md border border-kairos-danger px-3 py-1.5 text-sm
                text-kairos-danger hover:bg-red-50 disabled:opacity-50"
            >
              {revokeBusy ? 'Revoking…' : 'Revoke'}
            </button>
          </div>
        </label>
        {revokeResult && (
          <p role="status" className="mt-1 text-sm text-green-700">
            {revokeResult}
          </p>
        )}
        {revokeError && (
          <p role="alert" className="mt-1 text-sm text-kairos-danger">
            {revokeError}
          </p>
        )}
      </div>
    </div>
  )
}
