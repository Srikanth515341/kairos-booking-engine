import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { createResource, updateResource } from '../api/admin'
import { ApiError } from '../api/errors'
import { listResources } from '../api/resources'
import type { Resource, ResourceCreateInput, ResourceUpdateInput } from '../api/types'
import { ResourceAdminsForm } from './ResourceAdminsForm'
import { ResourceForm } from './ResourceForm'

export function ResourceManagementPage() {
  const [cursorStack, setCursorStack] = useState<(string | undefined)[]>([undefined])
  const [pageIndex, setPageIndex] = useState(0)
  const [resources, setResources] = useState<Resource[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  const [creating, setCreating] = useState(false)
  const [createBusy, setCreateBusy] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  const [editingId, setEditingId] = useState<string | null>(null)
  const [editBusy, setEditBusy] = useState(false)
  const [editError, setEditError] = useState<string | null>(null)

  const [managingAdminsId, setManagingAdminsId] = useState<string | null>(null)

  const [statusBusyId, setStatusBusyId] = useState<string | null>(null)
  const [statusError, setStatusError] = useState<Record<string, string>>({})

  const cursor = cursorStack[pageIndex]

  const loadPage = useCallback(() => {
    setLoading(true)
    setLoadError(null)
    listResources(cursor !== undefined ? { cursor } : {})
      .then((page) => {
        setResources(page.data)
        setNextCursor(page.next_cursor)
      })
      .catch(() => {
        setLoadError('Could not load resources.')
      })
      .finally(() => {
        setLoading(false)
      })
  }, [cursor])

  useEffect(() => {
    loadPage()
  }, [loadPage])

  function goNext() {
    if (!nextCursor) return
    setCursorStack((current) => [...current.slice(0, pageIndex + 1), nextCursor])
    setPageIndex((current) => current + 1)
  }

  function goPrevious() {
    setPageIndex((current) => Math.max(0, current - 1))
  }

  async function handleCreate(input: ResourceCreateInput) {
    setCreateBusy(true)
    setCreateError(null)
    try {
      await createResource(input)
      setCreating(false)
      loadPage()
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'permission_denied') {
        setCreateError('You need system administrator access to create a resource.')
      } else if (caught instanceof ApiError) {
        setCreateError(caught.message)
      } else {
        setCreateError('Could not create this resource.')
      }
    } finally {
      setCreateBusy(false)
    }
  }

  async function handleEdit(resourceId: string, input: ResourceUpdateInput) {
    setEditBusy(true)
    setEditError(null)
    try {
      await updateResource(resourceId, input)
      setEditingId(null)
      loadPage()
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'permission_denied') {
        setEditError(
          'You need to administer this resource, or be a system administrator, to edit it.',
        )
      } else if (caught instanceof ApiError) {
        setEditError(caught.message)
      } else {
        setEditError('Could not save these changes.')
      }
    } finally {
      setEditBusy(false)
    }
  }

  async function handleToggleStatus(resource: Resource) {
    setStatusBusyId(resource.id)
    setStatusError((current) => ({ ...current, [resource.id]: '' }))
    const nextStatus = resource.status === 'active' ? 'inactive' : 'active'
    try {
      await updateResource(resource.id, { status: nextStatus })
      loadPage()
    } catch (caught) {
      setStatusError((current) => ({
        ...current,
        [resource.id]:
          caught instanceof ApiError && caught.code === 'permission_denied'
            ? 'You need to administer this resource, or be a system administrator, to change its status.'
            : caught instanceof ApiError
              ? caught.message
              : 'Could not change status.',
      }))
    } finally {
      setStatusBusyId(null)
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-gray-900">Resource Management</h1>
        <button
          type="button"
          onClick={() => {
            setCreating((current) => !current)
          }}
          className="rounded-md bg-kairos-primary px-4 py-2 text-sm font-medium text-white
            hover:bg-kairos-primary-dark"
        >
          {creating ? 'Cancel' : 'New resource'}
        </button>
      </div>
      <p className="mt-1 text-sm text-gray-500">
        Any authenticated user can see this page — creating, editing, and admin grants are gated
        server-side (`system_admin`, or the resource&rsquo;s own admins). A 403 here means exactly
        that, not a bug.
      </p>

      {creating && (
        <ResourceForm
          mode="create"
          onSubmit={(input) => {
            void handleCreate(input)
          }}
          busy={createBusy}
          error={createError}
        />
      )}

      {loading && <p className="mt-4 text-sm text-gray-500">Loading…</p>}
      {loadError && (
        <p role="alert" className="mt-4 text-sm text-kairos-danger">
          {loadError}
        </p>
      )}

      <ul className="mt-4 divide-y divide-gray-200 rounded-md border border-gray-200 bg-white">
        {!loading && resources.length === 0 && (
          <li className="p-4 text-sm text-gray-500">No resources found.</li>
        )}
        {resources.map((resource) => (
          <li key={resource.id} className="p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-900">{resource.name}</p>
                <p className="text-sm text-gray-600">
                  {resource.category} · {resource.timezone} ·{' '}
                  {resource.bookable_start_time.slice(0, 5)}–
                  {resource.bookable_end_time.slice(0, 5)}
                </p>
                <span
                  className={`mt-1 inline-block rounded-full px-2 py-0.5 text-xs ${
                    resource.status === 'active'
                      ? 'bg-green-100 text-green-700'
                      : 'bg-gray-200 text-gray-600'
                  }`}
                >
                  {resource.status}
                </span>
              </div>
              <div className="flex flex-wrap justify-end gap-2">
                <Link
                  to={`/admin/utilization?resourceId=${resource.id}`}
                  className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700
                    hover:bg-gray-50"
                >
                  Utilization
                </Link>
                <button
                  type="button"
                  onClick={() => {
                    setManagingAdminsId((current) => (current === resource.id ? null : resource.id))
                    setEditingId(null)
                  }}
                  className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700
                    hover:bg-gray-50"
                >
                  Admins
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setEditingId((current) => (current === resource.id ? null : resource.id))
                    setManagingAdminsId(null)
                    setEditError(null)
                  }}
                  className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700
                    hover:bg-gray-50"
                >
                  Edit
                </button>
                <button
                  type="button"
                  disabled={statusBusyId === resource.id}
                  onClick={() => {
                    void handleToggleStatus(resource)
                  }}
                  className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700
                    hover:bg-gray-50 disabled:opacity-50"
                >
                  {resource.status === 'active' ? 'Take offline' : 'Reactivate'}
                </button>
              </div>
            </div>

            {statusError[resource.id] && (
              <p role="alert" className="mt-2 text-sm text-kairos-danger">
                {statusError[resource.id]}
              </p>
            )}

            {editingId === resource.id && (
              <ResourceForm
                mode="edit"
                resource={resource}
                onSubmit={(input) => {
                  void handleEdit(resource.id, input)
                }}
                onCancel={() => {
                  setEditingId(null)
                  setEditError(null)
                }}
                busy={editBusy}
                error={editError}
              />
            )}

            {managingAdminsId === resource.id && (
              <ResourceAdminsForm
                resourceId={resource.id}
                onClose={() => {
                  setManagingAdminsId(null)
                }}
              />
            )}
          </li>
        ))}
      </ul>

      <div className="mt-4 flex justify-between">
        <button
          type="button"
          disabled={pageIndex === 0}
          onClick={goPrevious}
          className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700
            hover:bg-gray-50 disabled:opacity-50"
        >
          Previous
        </button>
        <button
          type="button"
          disabled={!nextCursor}
          onClick={goNext}
          className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700
            hover:bg-gray-50 disabled:opacity-50"
        >
          Next
        </button>
      </div>
    </div>
  )
}
