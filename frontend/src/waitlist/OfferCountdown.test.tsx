import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { OfferCountdown } from './OfferCountdown'

describe('OfferCountdown', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-01T12:00:00Z'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows the remaining time, and ticks down a second at a time', () => {
    render(<OfferCountdown expiresAt="2026-09-01T12:02:30Z" />)
    expect(screen.getByText(/2:30 remaining/)).toBeInTheDocument()

    act(() => {
      vi.advanceTimersByTime(90 * 1000)
    })
    expect(screen.getByText(/1:00 remaining/)).toBeInTheDocument()
  })

  it('labels the remaining time as approximate — the countdown is advisory, never authoritative (Spec v1.0 §5.13)', () => {
    render(<OfferCountdown expiresAt="2026-09-01T12:05:00Z" />)
    expect(screen.getByText(/approximate/)).toBeInTheDocument()
  })

  it('once visually expired, still reads as an expected outcome to check, not a hard stop', () => {
    render(<OfferCountdown expiresAt="2026-09-01T12:00:05Z" />)
    act(() => {
      vi.advanceTimersByTime(10 * 1000)
    })
    expect(screen.getByText(/may have expired — try confirming or declining/)).toBeInTheDocument()
  })
})
