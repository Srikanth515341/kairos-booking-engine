"""Shared view infrastructure. Every endpoint requires authentication
(Spec v1.0 §1: "No anonymous endpoints") — declared once here rather than
repeated on every view.
"""

from __future__ import annotations

from typing import Any, NoReturn, cast

from django.http import HttpRequest, HttpResponse
from django.views import View
from rest_framework import exceptions as drf_exceptions
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from kairos.core.models import AlertEvent, SystemCheckRun
from kairos.identity.authentication import OIDCSessionAuthentication, StubUserIdAuthentication
from kairos.identity.authorization import AuthorizationService
from kairos.identity.models import AppUser


class KairosAPIView(APIView):
    # OIDC first (Phase 9) — it's the real mechanism and, per DRF's
    # get_authenticate_header(), the FIRST authenticator's challenge is
    # what an unauthenticated 401 carries, so a `Bearer` challenge (not
    # `X-Dev-User-Id`) is the correct default now. StubUserIdAuthentication
    # is a no-op everywhere except kairos.settings.test regardless of its
    # position here — see that class's docstring.
    authentication_classes = [OIDCSessionAuthentication, StubUserIdAuthentication]
    permission_classes = [IsAuthenticated]

    def throttled(self, request: Request, wait: float | None) -> NoReturn:
        """DRF's own `check_throttles` doesn't tell `throttled()` WHICH
        throttle class failed — only the max `wait` across all of them.
        Each throttle in `kairos.core.rate_limit` sets `request.
        _kairos_throttle_cause` itself right before returning False from
        `allow_request`, so this override can attach that onto the raised
        exception (Implementation Plan Phase 22) — the same `cause`-
        carrying convention `ServiceUnavailableError`/`AuthenticationFailed
        (code=...)` already established in Phase 21, read back by
        `kairos.core.drf.kairos_exception_handler` and, from there, by
        `MetricsMiddleware` for "rate-limit trigger rate by cause."
        """
        # `request` here is the SAME DRF `Request` instance `allow_request`
        # received (DRF passes one Request through `initial()` ->
        # `check_throttles()` -> `throttled()` unchanged) — the throttle
        # set this attribute directly on it, not on the underlying Django
        # HttpRequest, so read it back the same way.
        exc = drf_exceptions.Throttled(wait=wait)
        exc.cause = getattr(request, "_kairos_throttle_cause", "rate_limited")  # type: ignore[attr-defined]
        raise exc


def request_id(request: Request) -> str:
    """Extracted here (Implementation Plan Phase 19) once a fourth view
    module (kairos.identity, for the deactivate endpoint) needed the
    identical helper `bookings`/`waitlist`/`resources` views.py already
    each defined locally — this project's own "worth extracting once
    real duplication exists, not before" threshold (see
    RecordableConflictError, Phase 16).
    """
    django_request = getattr(request, "_request", request)
    return getattr(django_request, "request_id", "") or ""


# Spec v1.0 §5.15's own example response lists the six checks in this
# order (schema_assertion and reconciliation first, the four background-
# job heartbeats after) — deliberately NOT `SystemCheckName.values`'
# declared enum order (RECONCILIATION, SCHEMA_ASSERTION, HOLD_REAPER,
# OFFER_CASCADE, SERIES_MATERIALIZATION, TZDATA_REMATERIALIZATION), which
# differs, since this is a human-facing dashboard-style read where the
# two most load-bearing checks belong first.
_CHECKS_LATEST_ORDER = (
    "schema_assertion",
    "reconciliation",
    "hold_reaper",
    "offer_cascade",
    "series_materialization",
    "tzdata_rematerialization",
)


def _require_operations_or_system_admin(user: AppUser) -> None:
    """Extracted (Implementation Plan Phase 21) once `AdminDashboardView`
    became a second view needing the IDENTICAL gate `AdminChecksLatestView`
    already had inline — this project's own "worth extracting once real
    duplication exists, not before" threshold (`RecordableConflictError`,
    Phase 16; `request_id()`, Phase 19).
    """
    if not (AuthorizationService.is_operations(user) or AuthorizationService.is_system_admin(user)):
        raise drf_exceptions.PermissionDenied()


def _latest_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for check_name in _CHECKS_LATEST_ORDER:
        latest = SystemCheckRun.objects.filter(check_name=check_name).order_by("-run_at").first()
        if latest is None:
            # Absence IS the signal (RFC v1.0 §14) — reported honestly as
            # null/null rather than omitted or faked as a pass, so a
            # dashboard can't mistake "never run" for "healthy."
            checks.append(
                {"check_name": check_name, "last_run_at": None, "status": None, "findings": {}}
            )
        else:
            checks.append(
                {
                    "check_name": check_name,
                    "last_run_at": latest.run_at.isoformat().replace("+00:00", "Z"),
                    "status": latest.status,
                    "findings": latest.findings,
                }
            )
    return checks


class AdminChecksLatestView(KairosAPIView):
    """GET /api/v1/admin/checks/latest (Spec v1.0 §5.15; Implementation
    Plan Phase 20) — read-only surface over `system_check_run`, for
    `operations` or `system_admin`. Exists so dashboards don't need
    direct database access (Spec's own stated reason) — the jobs
    themselves already query Postgres directly and don't need this
    endpoint to function.
    """

    def get(self, request: Request) -> Response:
        user = cast(AppUser, request.user)
        _require_operations_or_system_admin(user)
        return Response({"checks": _latest_checks()})


class AdminDashboardView(KairosAPIView):
    """GET /api/v1/admin/dashboard (Implementation Plan Phase 21; Rollout
    v1.0 §6.1/§6.2) — everything `GET /admin/checks/latest` shows PLUS
    open alerts and every Rollout v1.0 §6.2 metric, in one response, for
    `operations`/`system_admin`. The self-contained HTML dashboard page
    (`AdminDashboardPageView`, no new frontend framework or separate
    service) is a thin client-side poller against exactly this endpoint —
    this IS "the dashboard," not a summary of it.
    """

    def get(self, request: Request) -> Response:
        user = cast(AppUser, request.user)
        _require_operations_or_system_admin(user)

        from kairos.core.constants import METRICS_WINDOW_SECONDS
        from kairos.core.metrics import (
            audit_actor_unknown_count,
            auth_failure_rate_by_shape,
            error_rate_by_cause,
            idempotency_key_stats,
            p95_duration_ms,
            rate_limit_metric_slot,
            redis_availability,
        )
        from kairos.core.models import RequestMetric

        open_alerts = [
            {
                "alert_key": a.alert_key,
                "severity": a.severity,
                "message": a.message,
                "context": a.context,
                "fired_at": a.fired_at.isoformat().replace("+00:00", "Z"),
                "email_status": a.email_status,
            }
            for a in AlertEvent.objects.filter(resolved_at__isnull=True).order_by("-fired_at")
        ]

        return Response(
            {
                "checks": _latest_checks(),
                "open_alerts": open_alerts,
                "metrics": {
                    "window_seconds": METRICS_WINDOW_SECONDS,
                    "booking_write_p95_ms": p95_duration_ms(
                        metric_type=RequestMetric.Type.BOOKING_WRITE
                    ),
                    "availability_read_p95_ms": p95_duration_ms(
                        metric_type=RequestMetric.Type.AVAILABILITY_READ
                    ),
                    "error_503": error_rate_by_cause(),
                    "auth_failures": auth_failure_rate_by_shape(),
                    "redis_available": redis_availability(),
                    "idempotency": idempotency_key_stats(),
                    "audit_actor_unknown_count": audit_actor_unknown_count(),
                    "rate_limiting": rate_limit_metric_slot(),
                },
            }
        )


_DASHBOARD_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Kairos Operations Dashboard</title>
<style>
  body {
    font-family: -apple-system, Segoe UI, sans-serif; margin: 2rem;
    background: #0b0e14; color: #d8dee9;
  }
  h1 { font-size: 1.25rem; }
  .row { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
  .card {
    background: #151a24; border: 1px solid #2a3140; border-radius: 8px;
    padding: 1rem; min-width: 220px; flex: 1;
  }
  .card h2 { font-size: 0.85rem; text-transform: uppercase; color: #8b96ab; margin: 0 0 0.5rem; }
  .pass { color: #4caf7d; } .fail { color: #e05d5d; }
  .stale { color: #e0a95d; } .unknown { color: #8b96ab; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  td, th { text-align: left; padding: 0.25rem 0.5rem; border-bottom: 1px solid #2a3140; }
  input, button {
    font-size: 0.85rem; padding: 0.4rem; border-radius: 4px;
    border: 1px solid #2a3140; background: #0b0e14; color: #d8dee9;
  }
  button { cursor: pointer; }
  #status { font-size: 0.8rem; color: #8b96ab; }
  pre {
    white-space: pre-wrap; font-size: 0.75rem; background: #0b0e14;
    padding: 0.5rem; border-radius: 4px;
  }
</style>
</head>
<body>
<h1>Kairos Operations Dashboard</h1>
<p>
  Bearer token: <input id="token" type="password" size="40" placeholder="paste a session token">
  <button id="load">Load</button>
  <button id="auto">Auto-refresh: off</button>
  <span id="status"></span>
</p>
<div class="row">
  <div class="card"><h2>Checks</h2><table id="checks"></table></div>
  <div class="card"><h2>Open Alerts</h2><table id="alerts"></table></div>
</div>
<div class="row">
  <div class="card"><h2>Latency (P95)</h2><table id="latency"></table></div>
  <div class="card"><h2>503 rate by cause</h2><table id="e503"></table></div>
  <div class="card"><h2>Auth failures by shape</h2><table id="auth"></table></div>
  <div class="card"><h2>Rate limiting (429s)</h2><table id="ratelimit"></table></div>
  <div class="card"><h2>Other</h2><table id="other"></table></div>
</div>
<h2>Raw response</h2>
<pre id="raw">(not loaded yet)</pre>
<script>
let timer = null;
function statusClass(s) { return s === 'pass' ? 'pass' : s === 'fail' ? 'fail' : 'unknown'; }
function row(cells) { return '<tr>' + cells.map(c => '<td>' + c + '</td>').join('') + '</tr>'; }

async function load() {
  const token = document.getElementById('token').value.trim();
  const statusEl = document.getElementById('status');
  if (!token) { statusEl.textContent = 'paste a bearer token first'; return; }
  statusEl.textContent = 'loading...';
  try {
    const resp = await fetch('/api/v1/admin/dashboard', {
      headers: { 'Authorization': 'Bearer ' + token },
    });
    if (!resp.ok) { statusEl.textContent = 'HTTP ' + resp.status; return; }
    const data = await resp.json();
    statusEl.textContent = 'updated ' + new Date().toLocaleTimeString();
    document.getElementById('raw').textContent = JSON.stringify(data, null, 2);

    document.getElementById('checks').innerHTML = data.checks.map(c => {
      const text = c.status || 'never run';
      const label = '<span class="' + statusClass(c.status) + '">' + text + '</span>';
      return row([c.check_name, label, c.last_run_at || '-']);
    }).join('');

    document.getElementById('alerts').innerHTML = data.open_alerts.length
      ? data.open_alerts.map(a => row([a.severity, a.alert_key, a.email_status])).join('')
      : row(['none open', '', '']);

    const m = data.metrics;
    document.getElementById('latency').innerHTML =
      row(['booking_write P95', (m.booking_write_p95_ms ?? '-') + ' ms']) +
      row(['availability_read P95', (m.availability_read_p95_ms ?? '-') + ' ms']);

    document.getElementById('e503').innerHTML =
      row(['rate', (m.error_503.rate * 100).toFixed(2) + '%']) +
      Object.entries(m.error_503.by_cause).map(([k, v]) => row([k, v])).join('');

    document.getElementById('auth').innerHTML =
      Object.entries(m.auth_failures.by_shape).length
        ? Object.entries(m.auth_failures.by_shape).map(([k, v]) => row([k, v])).join('')
        : row(['none', '']);

    const rl = m.rate_limiting;
    if (rl.available) {
      const principalsText = rl.top_principals.length
        ? rl.top_principals.map(p => p.principal_id.slice(0, 8) + ':' + p.count).join(', ')
        : 'none';
      document.getElementById('ratelimit').innerHTML =
        row(['total 429s', rl.total_429]) +
        row(['rate', (rl.rate * 100).toFixed(2) + '%']) +
        Object.entries(rl.by_cause).map(([k, v]) => row([k, v])).join('') +
        row(['top principals', principalsText]);
    } else {
      document.getElementById('ratelimit').innerHTML = row(['status', rl.note]);
    }

    document.getElementById('other').innerHTML =
      row(['redis', m.redis_available ? 'up' : 'down']) +
      row(['idempotency keys', m.idempotency.total_keys]) +
      row(['idempotency cleanup last run', m.idempotency.cleanup_last_run_at || 'never']) +
      row(['audit actor_type=unknown', m.audit_actor_unknown_count]);
  } catch (e) {
    statusEl.textContent = 'error: ' + e;
  }
}

document.getElementById('load').addEventListener('click', load);
document.getElementById('auto').addEventListener('click', function () {
  if (timer) { clearInterval(timer); timer = null; this.textContent = 'Auto-refresh: off'; }
  else { timer = setInterval(load, 30000); this.textContent = 'Auto-refresh: on (30s)'; load(); }
});
</script>
</body>
</html>
"""


class AdminDashboardPageView(View):
    """GET /admin/dashboard/ — a single self-contained HTML page (no
    template engine: `TEMPLATES = []`, Implementation Plan Phase 4's own
    "JSON only" scope, so this is a raw string response, not a rendered
    template), no new frontend framework, no separate service (per this
    phase's own explicit instruction). Carries no DRF authentication of
    its own — it is an empty shell that polls the AUTHENTICATED
    `GET /api/v1/admin/dashboard` JSON endpoint client-side, using a
    bearer token the operator pastes in (never persisted — held only in
    the page's own JS state) — the same "no frontend until Phase 23"
    boundary every other phase has respected, applied to an internal ops
    tool rather than invented as an exception to it.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse(_DASHBOARD_PAGE_HTML, content_type="text/html")
