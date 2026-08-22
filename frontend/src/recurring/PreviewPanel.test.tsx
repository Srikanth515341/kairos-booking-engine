import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ComponentProps } from 'react'
import { describe, expect, it, vi } from 'vitest'
import type { RecurringPreviewResponse } from '../api/types'
import { PreviewPanel } from './PreviewPanel'

const PREVIEW: RecurringPreviewResponse = {
  preview_token: 'pv_test',
  resource_id: 'res-1',
  tzdata_version: '2026.3',
  would_create: [
    { occurrence_date: '2026-09-01', start: '2026-09-01T13:00:00Z', end: '2026-09-01T13:30:00Z' },
  ],
  conflicts: [
    {
      occurrence_date: '2026-09-08',
      start: '2026-09-08T13:00:00Z',
      end: '2026-09-08T13:30:00Z',
      reason: 'slot_unavailable',
    },
  ],
  time_adjustments: [
    {
      occurrence_date: '2027-03-28',
      issue: 'nonexistent_local_time',
      requested_local: '02:30:00',
      adjusted_local: '03:30:00',
      explanation:
        'Local time does not exist due to a DST transition; shifted forward by the transition gap.',
    },
  ],
}

function renderPanel(overrides: Partial<ComponentProps<typeof PreviewPanel>> = {}) {
  const props = {
    preview: PREVIEW,
    timeZone: 'UTC',
    acknowledgedConflicts: new Set<string>(),
    acknowledgedAdjustments: new Set<string>(),
    onToggleConflict: vi.fn(),
    onToggleAdjustment: vi.fn(),
    onAcknowledgeAllConflicts: vi.fn(),
    onAcknowledgeAllAdjustments: vi.fn(),
    onConfirm: vi.fn(),
    onBack: vi.fn(),
    confirmBusy: false,
    confirmError: null,
    ...overrides,
  }
  render(<PreviewPanel {...props} />)
  return props
}

describe('PreviewPanel', () => {
  it('shows conflicts and time adjustments clearly, with the DST shift stated explicitly, not hidden', () => {
    renderPanel()
    expect(screen.getByText(/Conflicts — will NOT be created \(1\)/)).toBeInTheDocument()
    expect(
      screen.getByText(/Time adjustments — daylight saving transitions \(1\)/),
    ).toBeInTheDocument()
    // The DST-shifted occurrence's actual times must be visible in the text, not just a generic warning.
    expect(screen.getByText(/02:30/)).toBeInTheDocument()
    expect(screen.getByText(/03:30/)).toBeInTheDocument()
  })

  it('disables Confirm until every conflict AND every time adjustment is acknowledged', () => {
    renderPanel()
    expect(screen.getByRole('button', { name: 'Confirm series' })).toBeDisabled()
  })

  it('stays disabled with only the conflict acknowledged, not the adjustment', () => {
    renderPanel({ acknowledgedConflicts: new Set(['2026-09-08']) })
    expect(screen.getByRole('button', { name: 'Confirm series' })).toBeDisabled()
  })

  it('enables Confirm once both the conflict and the adjustment are acknowledged', () => {
    renderPanel({
      acknowledgedConflicts: new Set(['2026-09-08']),
      acknowledgedAdjustments: new Set(['2027-03-28']),
    })
    expect(screen.getByRole('button', { name: 'Confirm series' })).toBeEnabled()
  })

  it('never pre-checks acknowledgment boxes on the user’s behalf', () => {
    renderPanel()
    for (const checkbox of screen.getAllByRole('checkbox')) {
      expect(checkbox).not.toBeChecked()
    }
  })

  it('calls onToggleConflict when its checkbox is clicked, not onAcknowledgeAllConflicts', async () => {
    const user = userEvent.setup()
    const props = renderPanel()
    await user.click(screen.getByLabelText('Acknowledge conflict on 2026-09-08'))
    expect(props.onToggleConflict).toHaveBeenCalledWith('2026-09-08')
    expect(props.onAcknowledgeAllConflicts).not.toHaveBeenCalled()
  })

  it('"Acknowledge all" is a single explicit click, not automatic — and only offered while unacknowledged items remain', async () => {
    const user = userEvent.setup()
    const props = renderPanel()
    // Two sections (conflicts, adjustments) each offer their own
    // "Acknowledge all" — the conflicts section renders first.
    const buttons = screen.getAllByRole('button', { name: 'Acknowledge all' })
    expect(buttons).toHaveLength(2)
    const firstButton = buttons[0]
    expect(firstButton).toBeDefined()
    if (firstButton) {
      await user.click(firstButton)
    }
    expect(props.onAcknowledgeAllConflicts).toHaveBeenCalledTimes(1)
  })

  it('hides "Acknowledge all" for a section once every item in it is already acknowledged', () => {
    renderPanel({ acknowledgedConflicts: new Set(['2026-09-08']) })
    // Only the (still-unacknowledged) adjustments section offers it now.
    expect(screen.getAllByRole('button', { name: 'Acknowledge all' })).toHaveLength(1)
  })
})
