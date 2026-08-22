import { useEffect, useState } from 'react'

interface OfferCountdownProps {
  expiresAt: string
}

function formatRemaining(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes)}:${String(seconds).padStart(2, '0')}`
}

/**
 * `expires_at` is a UX aid, never the source of truth (Spec v1.0 §5.13:
 * "the client countdown is not authoritative") — expiry is enforced by
 * the server's own conditional UPDATE. This component only ever DISPLAYS
 * a countdown; it never disables or gates the Confirm/Decline actions —
 * those stay clickable even once the visible countdown reaches zero,
 * because a request already in flight can legitimately land a moment
 * before real expiry and succeed, and one landing a moment after is a
 * normal 409 `offer_expired`, not a bug, handled by the caller.
 */
export function OfferCountdown({ expiresAt }: OfferCountdownProps) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const intervalId = setInterval(() => {
      setNow(Date.now())
    }, 1000)
    return () => {
      clearInterval(intervalId)
    }
  }, [])

  const remainingMs = Date.parse(expiresAt) - now
  const visuallyExpired = remainingMs <= 0

  return (
    <span className={visuallyExpired ? 'text-kairos-danger' : 'text-gray-600'}>
      {visuallyExpired
        ? 'This offer may have expired — try confirming or declining to see current status.'
        : `~${formatRemaining(remainingMs)} remaining (approximate)`}
    </span>
  )
}
