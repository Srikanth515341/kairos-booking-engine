import { useState } from 'react'
import {
  cancelRecurringSeries,
  confirmRecurringSeries,
  previewRecurringSeries,
} from '../api/recurringBookings'
import { ApiError } from '../api/errors'
import type {
  RecurringConfirmResponse,
  RecurringPreviewRequest,
  RecurringPreviewResponse,
} from '../api/types'
import { getViewerTimeZone } from '../booking/timezone'
import { ConfirmResult } from './ConfirmResult'
import { PreviewPanel } from './PreviewPanel'
import { SeriesForm } from './SeriesForm'

type Step = 'form' | 'preview' | 'result'

export function RecurringSeriesPage() {
  const timeZone = getViewerTimeZone()

  const [step, setStep] = useState<Step>('form')
  const [previewBusy, setPreviewBusy] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [preview, setPreview] = useState<RecurringPreviewResponse | null>(null)

  const [acknowledgedConflicts, setAcknowledgedConflicts] = useState<Set<string>>(new Set())
  const [acknowledgedAdjustments, setAcknowledgedAdjustments] = useState<Set<string>>(new Set())
  const [confirmBusy, setConfirmBusy] = useState(false)
  const [confirmError, setConfirmError] = useState<string | null>(null)
  const [confirmResult, setConfirmResult] = useState<RecurringConfirmResponse | null>(null)

  async function handlePreview(input: RecurringPreviewRequest) {
    setPreviewBusy(true)
    setPreviewError(null)
    try {
      const result = await previewRecurringSeries(input)
      setPreview(result)
      setAcknowledgedConflicts(new Set())
      setAcknowledgedAdjustments(new Set())
      setStep('preview')
    } catch (caught) {
      setPreviewError(
        caught instanceof ApiError ? caught.message : 'Could not preview this series.',
      )
    } finally {
      setPreviewBusy(false)
    }
  }

  function toggleConflict(occurrenceDate: string) {
    setAcknowledgedConflicts((current) => {
      const next = new Set(current)
      if (next.has(occurrenceDate)) {
        next.delete(occurrenceDate)
      } else {
        next.add(occurrenceDate)
      }
      return next
    })
  }

  function toggleAdjustment(occurrenceDate: string) {
    setAcknowledgedAdjustments((current) => {
      const next = new Set(current)
      if (next.has(occurrenceDate)) {
        next.delete(occurrenceDate)
      } else {
        next.add(occurrenceDate)
      }
      return next
    })
  }

  async function handleConfirm() {
    if (!preview) return
    setConfirmBusy(true)
    setConfirmError(null)
    try {
      const result = await confirmRecurringSeries({
        preview_token: preview.preview_token,
        acknowledged_conflicts: Array.from(acknowledgedConflicts),
        acknowledged_adjustments: Array.from(acknowledgedAdjustments),
      })
      // A 207 with a non-empty conflicts array is a SUCCESS (Spec v1.0
      // §10) — it lands here unconditionally, never in the catch branch,
      // since `fetch`'s own Response.ok already treats 207 as 2xx.
      setConfirmResult(result)
      setStep('result')
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'preview_expired') {
        setConfirmError(
          'This preview has expired (previews are valid for 15 minutes). Please preview again.',
        )
      } else if (caught instanceof ApiError && caught.code === 'unacknowledged_conflicts') {
        setConfirmError('Please acknowledge every conflict and time adjustment before confirming.')
      } else if (caught instanceof ApiError) {
        setConfirmError(caught.message)
      } else {
        setConfirmError('Something went wrong. Please try again.')
      }
    } finally {
      setConfirmBusy(false)
    }
  }

  function handleStartNew() {
    setStep('form')
    setPreview(null)
    setConfirmResult(null)
    setPreviewError(null)
    setConfirmError(null)
    setAcknowledgedConflicts(new Set())
    setAcknowledgedAdjustments(new Set())
  }

  if (step === 'result' && confirmResult) {
    return (
      <ConfirmResult
        result={confirmResult}
        timeZone={timeZone}
        onCancelSeries={(reason) => cancelRecurringSeries(confirmResult.series_id, reason)}
        onStartNew={handleStartNew}
      />
    )
  }

  if (step === 'preview' && preview) {
    return (
      <PreviewPanel
        preview={preview}
        timeZone={timeZone}
        acknowledgedConflicts={acknowledgedConflicts}
        acknowledgedAdjustments={acknowledgedAdjustments}
        onToggleConflict={toggleConflict}
        onToggleAdjustment={toggleAdjustment}
        onAcknowledgeAllConflicts={() => {
          setAcknowledgedConflicts(new Set(preview.conflicts.map((c) => c.occurrence_date)))
        }}
        onAcknowledgeAllAdjustments={() => {
          setAcknowledgedAdjustments(
            new Set(preview.time_adjustments.map((a) => a.occurrence_date)),
          )
        }}
        onConfirm={() => {
          void handleConfirm()
        }}
        onBack={() => {
          setStep('form')
        }}
        confirmBusy={confirmBusy}
        confirmError={confirmError}
      />
    )
  }

  return (
    <SeriesForm
      onPreview={(input) => {
        void handlePreview(input)
      }}
      busy={previewBusy}
      error={previewError}
    />
  )
}
