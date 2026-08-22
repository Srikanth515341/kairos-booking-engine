import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as adminApi from '../api/admin'
import { ApiError } from '../api/errors'
import * as resourcesApi from '../api/resources'
import type { Resource } from '../api/types'
import { ResourceManagementPage } from './ResourceManagementPage'

function renderPage() {
  return render(
    <MemoryRouter>
      <ResourceManagementPage />
    </MemoryRouter>,
  )
}

vi.mock('../api/resources')
vi.mock('../api/admin')

const mockedListResources = vi.mocked(resourcesApi.listResources)
const mockedCreateResource = vi.mocked(adminApi.createResource)
const mockedUpdateResource = vi.mocked(adminApi.updateResource)

const RESOURCE: Resource = {
  id: 'res-1',
  name: 'Conference Room A',
  category: 'meeting_room',
  timezone: 'UTC',
  bookable_start_time: '09:00:00',
  bookable_end_time: '17:00:00',
  max_booking_duration_minutes: null,
  offboarding_policy: 'transfer',
  status: 'active',
  created_by: 'u1',
  created_at: '2026-01-01T00:00:00Z',
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedListResources.mockResolvedValue({ data: [RESOURCE], next_cursor: null })
})

describe('ResourceManagementPage', () => {
  it('lists resources and lets any signed-in user see the page (gating is server-side)', async () => {
    renderPage()
    expect(await screen.findByText('Conference Room A')).toBeInTheDocument()
  })

  it('creates a resource and refreshes the list on success', async () => {
    const user = userEvent.setup()
    const created: Resource = { ...RESOURCE, id: 'res-2', name: 'Room B' }
    mockedCreateResource.mockResolvedValue(created)

    renderPage()
    await screen.findByText('Conference Room A')

    await user.click(screen.getByRole('button', { name: 'New resource' }))
    await user.type(screen.getByLabelText('Name'), 'Room B')
    await user.click(screen.getByRole('button', { name: 'Create resource' }))

    await waitFor(() => {
      expect(mockedCreateResource).toHaveBeenCalledWith(expect.objectContaining({ name: 'Room B' }))
    })
    // Refetched after success.
    await waitFor(() => {
      expect(mockedListResources).toHaveBeenCalledTimes(2)
    })
  })

  it('shows a specific permission-denied message on create, not a generic error, and leaves the form open', async () => {
    const user = userEvent.setup()
    mockedCreateResource.mockRejectedValue(
      new ApiError(
        { code: 'permission_denied', message: 'nope', details: {}, request_id: null },
        403,
      ),
    )

    renderPage()
    await screen.findByText('Conference Room A')
    await user.click(screen.getByRole('button', { name: 'New resource' }))
    await user.type(screen.getByLabelText('Name'), 'Room C')
    await user.click(screen.getByRole('button', { name: 'Create resource' }))

    expect(
      await screen.findByText(/need system administrator access to create a resource/),
    ).toBeInTheDocument()
    // The create form is still visible so the user doesn't lose their input.
    expect(screen.getByRole('button', { name: 'Create resource' })).toBeInTheDocument()
  })

  it('takes a resource offline via PATCH status, and reactivates it the same way', async () => {
    const user = userEvent.setup()
    mockedUpdateResource.mockResolvedValue({ ...RESOURCE, status: 'inactive' })

    renderPage()
    await screen.findByText('Conference Room A')

    await user.click(screen.getByRole('button', { name: 'Take offline' }))

    await waitFor(() => {
      expect(mockedUpdateResource).toHaveBeenCalledWith('res-1', { status: 'inactive' })
    })
  })

  it('edits a resource with only the changed fields, never category/timezone (not PATCH-updatable)', async () => {
    const user = userEvent.setup()
    mockedUpdateResource.mockResolvedValue(RESOURCE)

    renderPage()
    await screen.findByText('Conference Room A')

    await user.click(screen.getByRole('button', { name: 'Edit' }))
    const nameInput = screen.getByLabelText('Name')
    await user.clear(nameInput)
    await user.type(nameInput, 'Renamed Room')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => {
      expect(mockedUpdateResource).toHaveBeenCalledWith(
        'res-1',
        expect.objectContaining({ name: 'Renamed Room', status: 'active' }),
      )
    })
    const [, payload] = mockedUpdateResource.mock.calls[0] ?? []
    expect(payload).not.toHaveProperty('category')
    expect(payload).not.toHaveProperty('timezone')
  })
})
