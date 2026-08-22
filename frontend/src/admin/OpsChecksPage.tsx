import { useEffect, useState } from 'react'
import { getAdminChecksLatest } from '../api/admin'
import { ApiError } from '../api/errors'
import { ADMIN_CHECK_NAMES, type AdminCheck, type ReconciliationFindings } from '../api/types'
import { formatDateTime, getViewerTimeZone } from '../booking/timezone'
import { CHECK_EXPLANATIONS, CHECK_LABELS } from './checkExplanations'

function statusBadgeClasses(status: AdminCheck['status']): string {
  if (status === 'pass') return 'bg-green-100 text-green-700'
  if (status === 'fail') return 'bg-red-100 text-red-700'
  return 'bg-gray-200 text-gray-600'
}

function statusLabel(status: AdminCheck['status']): string {
  if (status === 'pass') return 'Pass'
  if (status === 'fail') return 'Fail'
  return 'Never run'
}

export function OpsChecksPage() {
  const timeZone = getViewerTimeZone()
  const [checks, setChecks] = useState<AdminCheck[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getAdminChecksLatest()
      .then((response) => {
        if (!cancelled) {
          setChecks(response.checks)
          setLoading(false)
        }
      })
      .catch((caught: unknown) => {
        if (cancelled) return
        if (caught instanceof ApiError && caught.code === 'permission_denied') {
          setError('You need operations or system administrator access to view this dashboard.')
        } else {
          setError('Could not load the correctness checks.')
        }
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const byName = new Map((checks ?? []).map((check) => [check.check_name, check]))

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Operations — Correctness Checks</h1>
      <p className="mt-1 max-w-2xl text-sm text-gray-500">
        A read-only view over the six background jobs that continuously prove the booking guarantee
        still holds. A check that has never run is shown honestly as such, never as a false pass.
      </p>

      {loading && <p className="mt-4 text-sm text-gray-500">Loading…</p>}
      {error && (
        <p role="alert" className="mt-4 text-sm text-kairos-danger">
          {error}
        </p>
      )}

      {checks && (
        <ul className="mt-4 space-y-3">
          {ADMIN_CHECK_NAMES.map((name) => {
            const check = byName.get(name)
            const status = check?.status ?? null
            const reconciliationFindings =
              name === 'reconciliation'
                ? (check?.findings as ReconciliationFindings | undefined)
                : undefined

            return (
              <li key={name} className="rounded-md border border-gray-200 bg-white p-4">
                <div className="flex items-center justify-between">
                  <h2 className="text-sm font-semibold text-gray-900">{CHECK_LABELS[name]}</h2>
                  <span
                    className={`inline-block rounded-full px-2 py-0.5 text-xs ${statusBadgeClasses(status)}`}
                  >
                    {statusLabel(status)}
                  </span>
                </div>
                <p className="mt-1 text-xs text-gray-500">
                  {check?.last_run_at
                    ? `Last run: ${formatDateTime(check.last_run_at, timeZone)}`
                    : 'Never run.'}
                </p>
                {/* PRD/Spec v1.0 §5.15 + Phase 27 Scope IN: the correct
                    interpretation is shown for EVERY check, not just on
                    failure, so an operator already knows what a row means
                    before an incident forces them to read it under
                    pressure. */}
                <p className="mt-2 text-sm text-gray-600">{CHECK_EXPLANATIONS[name]}</p>

                {status === 'fail' && reconciliationFindings?.message && (
                  <div
                    role="alert"
                    className="mt-3 rounded-md border border-kairos-danger bg-red-50 p-3"
                  >
                    <p className="text-sm font-medium text-kairos-danger">
                      This means the guarantee has been REMOVED — not that a race occurred.
                    </p>
                    <p className="mt-1 text-sm text-red-800">{reconciliationFindings.message}</p>
                    {reconciliationFindings.overlaps_found !== undefined && (
                      <p className="mt-1 text-xs text-red-700">
                        Overlapping pairs found: {reconciliationFindings.overlaps_found}
                      </p>
                    )}
                  </div>
                )}

                {status === 'fail' && name !== 'reconciliation' && (
                  <p role="alert" className="mt-3 text-sm text-kairos-danger">
                    This check is currently failing — see the alert dashboard / on-call channel for
                    detail.
                  </p>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
