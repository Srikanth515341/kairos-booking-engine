# CLAUDE.md — Project Kairos

## Project Overview

Kairos is a concurrency-safe resource booking engine. The core differentiator: no two
overlapping bookings for the same resource can ever both succeed, because the guarantee is
enforced by a PostgreSQL exclusion constraint at the schema level — not by application
check-then-insert logic — so no code path, present or future, can bypass it.

## Source Documents

All six approved planning documents live under `docs/`, committed verbatim:

| # | Document | Path |
|---|---|---|
| 1 | PRD v1.0 | `docs/01-prd.md` |
| 2 | RFC / Technical Design Doc v1.0 | `docs/02-rfc.md` |
| 3 | API & Data Design Spec v1.0 | `docs/03-api-data-spec.md` |
| 4 | Test Plan / Acceptance Criteria v1.0 | `docs/04-test-plan.md` |
| 5 | Rollout & Runbook v1.0 | `docs/05-rollout-runbook.md` |
| 6 | Implementation Plan v1.0 | `docs/06-implementation-plan.md` |

The Implementation Plan (`docs/06-implementation-plan.md`) is the document actually being
executed against, one phase per session. Read its §1 (Project-Wide Rules) before starting
any phase.

## Current Architecture State

`infra/docker-compose.yml` runs PostgreSQL 16 with `btree_gist` enabled and
`max_connections=600` (spike/test setting — see the file's comment); it also provisions a
`kairos_test` database via `infra/init-test-db.sql`. `scripts/spike/` is throwaway spike code
(RFC §16) that produced `docs/spikes/S1-postgres-verification.md` and does not become the
application.

A Django project now exists under `backend/kairos/`, structured per RFC §4.1:

```
kairos-booking-engine/
├── .github/workflows/    # ci.yml — lint, test, concurrency (all real, Phase 3)
├── docs/                 # the six source documents + spike reports, checklists
├── backend/
│   ├── kairos/
│   │   ├── settings/      # base.py, dev.py, test.py, prod.py — DRF + logging wired in (Phase 4)
│   │   ├── core/           # constants.py, exceptions.py, drf.py, logging.py, middleware.py,
│   │   │                   # db.py (write-path session settings + audit actor propagation,
│   │   │                   # ONE shared apply_write_path_session_settings — Phase 8),
│   │   │                   # idempotency.py (Phase 5), models.py (IdempotencyKey since Phase 5,
│   │   │                   # AuditLog since Phase 8), migrations/0002-0003 (audit_log table,
│   │   │                   # kairos_app role + grants, write_audit_log() trigger — Phase 8),
│   │   │                   # management/commands/cleanup_idempotency_keys.py
│   │   ├── identity/       # app_user, resource_admin (UUID surrogate PK since Phase 8 —
│   │   │                   # was an implicit BigAutoField), authentication.py (# STUB, Phase 4),
│   │   │                   # authorization.py (is_resource_admin/is_operations, Phase 6)
│   │   ├── resources/      # resource, serializers.py, views.py, urls.py (list/detail/
│   │   │                   # availability — Phase 6; writes are Phase 19)
│   │   ├── bookings/       # booking, services.py (create/edit/cancel — Phase 7), serializers.py,
│   │   │                   # views.py (BookingHistoryView — Phase 8), urls.py
│   │   ├── urls.py, wsgi.py
│   ├── tests/
│   │   ├── test_booking_exclusion_smoke.py
│   │   ├── test_schema_assertion.py   # RECON-05 CI form — fails if the predicate is narrowed
│   │   ├── test_audit_trail.py        # AUD-01, AUD-02, grant/trigger-existence checks (Phase 8)
│   │   ├── conftest.py                # app_user / active_resource fixtures, shared
│   │   ├── bookings/                  # test_services.py, test_views.py, test_idempotency.py,
│   │   │                              # test_read_endpoints.py (Phase 6), test_cancel_edit.py
│   │   │                              # (Phase 7), test_history.py (AUD-03/04/05 — Phase 8)
│   │   ├── resources/                 # test_views.py (Phase 6 — list/detail/availability)
│   │   └── concurrency/               # Milestone 1 — the project's central proof
│   │       ├── harness.py             # barrier-released, independent-connection harness
│   │       ├── conftest.py
│   │       ├── test_conc_01.py        # identical-slot contention, N=200 x 10 runs
│   │       ├── test_conc_02.py        # partial + chained overlap
│   │       ├── test_conc_03.py        # edit-vs-create race (Phase 7)
│   │       ├── test_conc_04.py        # edit-vs-edit race (Phase 7)
│   │       └── test_conc_05.py        # cancel-and-rebook race
│   ├── manage.py
│   └── pyproject.toml      # ruff, mypy strict, pytest config
├── frontend/              # React + TypeScript — empty until Phase 23
├── infra/                 # docker-compose.yml, init-test-db.sql
└── scripts/
    └── spike/             # throwaway S1 spike scripts — will NOT be extended after Phase 1
```

Endpoints live (Phase 6 added the read path, Phase 7 the remaining single-booking mutations,
Phase 8 the history endpoint, to Phase 4/5's write path): `POST`/`GET /api/v1/bookings`,
`GET`/`PATCH /api/v1/bookings/{id}`, `POST /api/v1/bookings/{id}/cancel`,
`GET /api/v1/bookings/{id}/history`, `GET /api/v1/resources`, `GET /api/v1/resources/{id}`,
`GET /api/v1/resources/{id}/availability`. Every mutation (create, edit, cancel) is idempotent
(Phase 5/7 — `Idempotency-Key` is required; missing it is 400). Edit is owner-only, no admin
override; cancel is owner-or-resource-admin, with a reason required for the admin-override
case (400 otherwise). Cancelling an already-cancelled booking is a 200 no-op, independent of
idempotency key. No real auth (Phase 9 — currently a dev-only `X-Dev-User-Id` header stub,
clearly marked `# STUB` in `kairos/identity/authentication.py`).

Every state transition on `booking`/`resource`/`resource_admin` is audited (Phase 8): the
`write_audit_log()` trigger fires on every INSERT/UPDATE/DELETE against those three tables,
unconditionally — including a raw SQL write that never touches the service layer at all.
The RUNNING APPLICATION connects as `kairos_app`, a least-privilege role (ordinary DML on
every app table; `INSERT`/`SELECT`-only, never `UPDATE`/`DELETE`, on `audit_log` — enforced
by Postgres grants, not application code). Migrations still require the `kairos` superuser
DSN (`kairos_app` deliberately has no DDL rights) — see "Running Locally" below.
`backend/.venv/` is local and gitignored; recreate with
`python -m venv .venv && pip install -e ".[dev]"` from `backend/`.

## Completed Phases

| Phase | Name | One-line summary | Merged |
|---|---|---|---|
| 0 | Repository & Process Foundation | Scaffolding, six docs committed, CLAUDE.md/README.md initialized, CI skeleton | Pending (direct-to-main commit) |
| 1 | Spike S1 — Postgres Verification | All of RFC §16 S1.1–S1.7 verified against real PostgreSQL 16; gate PASSED; Candidate A confirmed. One liveness finding carried forward (see below) | Pending (on branch `phase-01-spike-postgres-verification`) |
| 2 | Core Schema & The Exclusion Constraint | Django project scaffolded (Django 6.1); `app_user`, `resource`, `resource_admin`, `booking` created via migrations; `no_overlapping_bookings` EXCLUDE constraint added via raw SQL with the Spec §3 comment block reproduced verbatim; smoke test confirms SQLSTATE 23P01 on sequential overlap; ruff + mypy strict pass with zero findings | Pending (on branch `phase-02-core-schema-exclusion-constraint`) |
| 3 | Concurrency Proof & CI Pipeline 🏁 Milestone 1 | Barrier-released concurrency harness (`tests/concurrency/harness.py`); CONC-01 (N=200, 10 runs), CONC-02 (partial + 5-way chained overlap), CONC-05 (cancel-and-rebook race) all pass reliably; RECON-05 CI-form schema assertion added and verified to fail on a manually narrowed predicate; full CI pipeline (`lint`, `test`, `concurrency` — three separate jobs) wired up; two new empirical findings beyond the carried-forward 40P01 one (see Key Technical Decisions) | Pending (on branch `phase-03-concurrency-proof-ci`) |
| 4 | Service Layer & Booking Creation API | DRF wired up (`/api/v1`, JSON only); `POST /api/v1/bookings` live with policy validation (bookable hours, max duration, past-dating, 365-day horizon), stub `X-Dev-User-Id` auth, `X-Request-Id` on every response, structured JSON logging, and the Spec §6 error envelope on every error path; `BookingService.create_booking` catches all four write-path SQLSTATEs specifically (23P01→409, 55P03/40P01/57014→503+Retry-After); verified live against the real dev server (not just the test client); all Phase 2/3 tests still pass | Pending (on branch `phase-04-booking-creation-api`) |
| 5 | Idempotency — The Transaction Boundary ⚠️ Subtle | `idempotency_key` table with a genuine composite `(user_id, key)` PRIMARY KEY (Django 6.1's `CompositePrimaryKey` — no surrogate-key workaround needed here, unlike Phase 2's `resource_admin`); `run_idempotent_write` (generic, in `core`, reused by every future write path) claims the key and runs the protected write in one transaction per RFC §11.2, recording a 409 outcome in its own follow-up transaction after rollback, and recording nothing at all for a 503 (outcome genuinely unknown); IDEM-01–04, 06 (100 reps), 09, 10, 11 all pass; verified live (replay returns the original booking, not a 409) | Pending (on branch `phase-05-idempotency`) |
| 6 | Read Path & Availability View | `GET /bookings/{id}` (owner/admin/operations, else 404 per Spec §1) and `GET /bookings` (cursor pagination, `idx_booking_user_starts`-shaped, held rows always excluded); `GET /resources`, `GET /resources/{id}` (read-only; writes are Phase 19); `GET /resources/{id}/availability` bounded to 92 days, `booking_id`/`owner` omitted entirely (not nulled) unless the requester owns the booking or administers the resource, held slots never reveal them to anyone (SEC-05); keyset (not offset) pagination proven stable under a concurrent insert between page fetches; N+1 guard verified via `django_assert_max_num_queries`; all prior tests still green (see the `MAX_ROUND_ATTEMPTS` finding below) | Pending (on branch `phase-06-read-path-availability`) |
| 7 | Cancellation & Editing | `PATCH /bookings/{id}` (owner only, evaluated against `no_overlapping_bookings` exactly as a create) and `POST /bookings/{id}/cancel` (owner or resource-admin override with a required reason, double-cancel idempotent at 200 regardless of idempotency key) — both share `_handle_write_database_error`'s SQLSTATE translation with create; the `transaction.on_commit()` waitlist-check stub registered inside cancel's nested atomic, correctly deferred to the outer (idempotency) transaction's commit; `BookingResponseSerializer` extended with `cancelled_at`/`cancelled_by`/`cancellation_reason`; idempotency fingerprints for both endpoints fold in `booking_id` (a real gap the body alone doesn't cover — see Key Technical Decisions); CONC-03 (edit-vs-create) and CONC-04 (edit-vs-edit), 10 runs each, loser verified unchanged at its original range. Also caught and fixed a real regression while doing this: Phase 5's session-settings fix had never actually been wired into `run_idempotent_write` — the key-claim INSERT was running with NO `lock_timeout` (proven via a spy test, then fixed, then proven fixed by reverting and watching the new test fail). Full suite (83 tests) green, including three concurrency runs back-to-back in one session | Merged (PR #7) |
| 8 | Audit Trail — Triggers & Grants ⚠️ Subtle | `audit_log` table + `write_audit_log()` trigger on `booking`/`resource`/`resource_admin`, firing unconditionally on every INSERT/UPDATE/DELETE — proven by a raw SQL write that never touches the service layer (AUD-02); a dedicated `kairos_app` database role holds ordinary DML on every app table but only `INSERT`/`SELECT` (never `UPDATE`/`DELETE`) on `audit_log`, enforced at the grant level and proven by actually connecting AS that role (AUD-01) — the RUNNING APPLICATION now connects as `kairos_app`, not the superuser, verified live via `manage.py runserver` and a full create→edit→cancel→history round trip over real HTTP; `app.actor_type`/`app.reason` propagate through the SAME shared `apply_write_path_session_settings` call as the write-path timeouts (not a second context manager), per explicit instruction after Phase 7's regression — verified by extending that exact regression test, not adding a parallel one; `GET /bookings/{id}/history` reconstructs full lifecycles via a genuine before/after field-level diff (`_compute_changes`), not just status transitions, since Phase 7's edit changes `time_range` while leaving `status` untouched — AUD-03(a)(b), AUD-04, AUD-05 all pass; AUD-03(d)'s "system-initiated write" has no real worker yet (Phase 16), so the underlying mechanism is proven directly instead. Three real bugs found and fixed via hands-on verification, not just passing tests: (1) `occurred_at` used `auto_now_add` (Python-side only) instead of a genuine `db_default`, so the trigger's raw INSERT — which never goes through Django's ORM — hit a NOT NULL violation; (2) `resource_admin`'s implicit `BigAutoField` surrogate PK couldn't satisfy the trigger's `COALESCE(NEW.id, OLD.id)` into `audit_log.entity_id UUID`, so it's now an explicit UUID PK like every other entity table; (3) `kairos_app` had no grant on Django's own `django_migrations` table, so the app failed to even START under the new role until caught by actually running `manage.py runserver`, not only the test suite. Full suite (96 tests) green | Pending (on branch `phase-08-audit-trail`) |

## Current Phase In Progress

None. Phase 8 is complete pending review and merge. Phase 9 (Authentication & Scoped
Authorization) is next.

## NOT Yet Built

No real authentication (Phase 9 — `X-Dev-User-Id` is an explicitly marked stub), no
Celery/Redis, no frontend, no hold reclamation (booking creation does not yet run the
cleanup-on-write DELETE from Spec §4.1 step 2, since `held` rows don't exist until Phase 15),
no `recurring_series`/`waitlist_entry`/`waitlist_offer`/`system_check_run` tables (each
arrives with the phase that needs it — see the Key Technical Decisions and Phase Index in
`docs/06-implementation-plan.md`). No replica routing (Phase 30 — `data_freshness` is
hardcoded `"primary"`, always true today since no replica exists). The audit trail covers
`booking`/`resource`/`resource_admin` only — `waitlist_entry`/`waitlist_offer` triggers arrive
with those tables (Phases 14/16), and `actor_type='unknown'` alerting (as opposed to just
recording the row) is Phase 21. The Phase 7 cancel endpoint's `on_commit()` hook still only
logs "would enqueue waitlist check" — the real worker dispatch is Phase 16, and there is no
`waitlist_entry` table yet for anything to be eligible against; correspondingly, AUD-03(d)'s
"system-initiated write" has no real worker to exercise yet (see Phase 8's row above — the
underlying trigger mechanism is proven directly instead). Resource CRUD (create/update/admin
grants) is Phase 19 — the Phase 6 resource endpoints are read-only, and no live code path
writes `resource_admin` yet (Phase 8's `ResourceAdmin` rows in tests are created directly via
the ORM, not through any endpoint). IDEM-05 (recurring replay) needs Phase 12's endpoint;
IDEM-07/08 (fault injection — process kill mid-transaction, proxy-level response drop) need
tooling that arrives in Phase 28; idempotency coverage on waitlist join/offer confirm arrives
with those endpoints (Phases 14, 16). `booking.series_id` does not exist yet — it cannot,
since `recurring_series` (Phase 11) doesn't exist; it is added in Phase 11, not retrofitted
early. `kairos_app`'s password is a hardcoded dev-only literal (Phase 8, matching
`infra/docker-compose.yml`'s own precedent) — Rollout (Phase 30) must replace it with a real
managed secret before any deployment. CONC-01's full 100-run + N=500 escalation and CONC-06
(throughput characterization) are deferred to Phase 28/29 respectively; CI only runs the
10-run CI-tier
reduction for every CONC test. The throwaway spike table `spike_booking` may still exist in
`kairos_dev`, created/dropped repeatedly by `scripts/spike/common.py` — unrelated to the real
schema. Do not assume any of the above exist in a fresh session — verify against this file
and `git log` first.

## Key Technical Decisions (with source references)

| Decision | Why | Source |
|---|---|---|
| PostgreSQL EXCLUDE constraint over distributed locking, `SELECT ... FOR UPDATE`, or SERIALIZABLE isolation | The guarantee lives at the schema level and cannot be bypassed by any code path, present or future — unlike alternatives where the guarantee depends on session config or application discipline | RFC v1.0 §3.3 |
| Constraint predicate covers `status IN ('confirmed', 'held')` | Waitlist holds must occupy the same exclusion domain as confirmed bookings, or a waitlist offer reserves nothing and an ordinary user can take the slot mid-offer | RFC v1.0 §10.1 |
| Idempotency key written in the same transaction as the booking insert | A separate transaction leaves a window where the booking commits and the key doesn't; the retry then tells the user their own successful booking is unavailable | RFC v1.0 §11.2 |
| Audit trail is trigger-based, not application-based | An application-level audit is opt-in per code path; a future bulk-import script would skip it. A trigger cannot be skipped by any writer | RFC v1.0 §12 |
| Two independent correctness monitors in production: schema assertion + reconciliation | Schema assertion detects the constraint being removed (the cause) before anyone is harmed; reconciliation detects an actual overlap (the consequence) if it somehow still happens | RFC v1.0 §14 |
| Hold expiry uses two mechanisms — cleanup-on-write AND a periodic reaper | A constraint predicate cannot reference `now()` (not IMMUTABLE), so expired holds must be actively reclaimed; cleanup-on-write makes the system self-healing even if the reaper stalls, but only the reaper drives cascade when there's no booking traffic | RFC v1.0 §10.4 |
| Recurring series store local wall-clock time + IANA zone identifier, not a fixed UTC offset | An offset can't express when DST rules change; each occurrence is independently converted to UTC using the rules in effect on its own date | RFC v1.0 §9.2 |
| Recurring series creation is a two-step preview → confirm flow | PRD FR33 requires the user to explicitly see and acknowledge which occurrences conflict, rather than silently creating a partial series | RFC v1.0 §5d, Spec v1.0 §5.8–5.9 |
| Waitlist eligibility is containment (`@>`), not overlap (`&&`) | The freed range must fully contain the entry's requested range — the strictest defensible rule, chosen because "next eligible user" admitted two readings in an earlier draft | PRD v1.0 FR21 |
| Spike S1 gate: PASSED. Candidate A (exclusion constraint) confirmed on real PostgreSQL 16 | `btree_gist` available; predicate accepted; blocking-not-fail-fast confirmed; `now()` in a predicate correctly rejected (42P17), confirming Phase 17's dual-reclamation design is necessary; cleanup-on-write showed zero deadlocks across 10,000 attempts | `docs/spikes/S1-postgres-verification.md` |
| `BookingService` (Phase 4) must treat SQLSTATE `40P01` (deadlock) the same as `55P03` (lock timeout) — 503 + retry, never a bare failure | Spike S1.2: at N=200 truly-simultaneous identical-slot contention (the extreme worst case), 2/10 runs produced deadlock cascades where the constraint's lack of fixed lock ordering (unlike a btree unique index) let a circular wait form. Safety held 10/10 (never more than one success) — only liveness was at risk, and it's retryable | `docs/spikes/S1-postgres-verification.md` §S1.2 Consequences |
| The EXCLUDE constraint is added via a raw-SQL migration (`RunSQL`), not Django's `ExclusionConstraint` ORM class | The Spec §3 comment block — the primary mitigation against RUNBOOK-01 cause #1 (someone narrowing the predicate during an unrelated migration) — needs to be reproduced verbatim at the point of definition; a raw SQL migration is where that text actually lives, byte for byte | Implementation Plan Phase 2 scope; RFC v1.0 §3.4 |
| `resource_admin`'s composite PK `(resource_id, user_id)` from Spec §3 is modeled as a surrogate `id` + `UniqueConstraint` in Django | Django's ORM ergonomics around composite primary keys are still immature; the surrogate key still enforces the identical one-grant-per-pair guarantee at the DB level — a Django-ergonomics deviation, not a correctness one | `kairos/identity/models.py` (`ResourceAdmin`) |
| Django's built-in `auth`/`contenttypes` apps are deliberately excluded from `INSTALLED_APPS` | Authentication is delegated to SSO/OIDC (RFC v1.0 §4, Phase 9) — Django's `User`/`Permission` machinery has no role here and would add unused tables that don't correspond to anything in Spec §3 | `kairos/settings/base.py` |
| `BookingService` (Phase 4) must also treat SQLSTATE `57014` (`query_canceled`, i.e. `statement_timeout` fired) the same as `55P03`/`40P01` — 503 + retry | Phase 3 CONC-01 empirical finding, reproducible at N=200: under the heaviest pileups, most losers don't block cleanly on one uncommitted competitor for a single >3s stretch (which `lock_timeout` would catch) — they accumulate many shorter waits under GiST index contention that together exceed `statement_timeout` (10s) before any one wait exceeds `lock_timeout`. Safety was unaffected in every observed run | `tests/concurrency/harness.py` (`EXPECTED_NONSUCCESS_SQLSTATES`) |
| At N=200 identical-slot contention, a single barrier-released round can — rarely — produce **zero** successes, not just "not exactly one": every competitor, including whichever would have won, can end up entangled in the same 57014/40P01 pileup | This is a liveness characteristic of the current, provisional timeout budget (RFC v1.0 §18 already flags `lock_timeout` as "tune from CONC-01's observed rate" — this is exactly that signal), not a safety violation. The concurrency tests retry a round only when it produced zero successes (bounded, `MAX_ROUND_ATTEMPTS = 3`); more than one success on any single attempt fails immediately and is never retried, so a real safety violation can never be masked | `tests/concurrency/test_conc_01.py`, `test_conc_02.py`, `test_conc_05.py` |
| CI's `concurrency` job starts Postgres via a plain `docker run` (`-c max_connections=600`), not the `services:` block used by the `test` job | GitHub Actions' `services:` block can't override a container's startup command, and CONC-01 alone opens 200 simultaneous connections — well past Postgres's default `max_connections=100`. Same setting as `infra/docker-compose.yml`, same reason (spike/test-scale concurrency, not production sizing) | `.github/workflows/ci.yml` |
| Write-path session settings and the audit actor variables are applied via `SELECT set_config(name, value, true)`, not literal `SET LOCAL ...` SQL text | `set_config`'s third argument (`is_local`) is the functional equivalent of `SET LOCAL`, but as a plain function call it safely accepts bind parameters — `actor_id`/`request_id` are request-influenced values, and interpolating them directly into `SET` statement text would be an injection risk `set_config` avoids entirely | `kairos/bookings/services.py` (`apply_write_path_session_settings`) |
| `PolicyValidationError` (custom, not DRF's `ValidationError`) carries a single `{"field", "issue"}` pair and is raised directly from `serializer.validate()`, stopping at the first violation | Spec v1.0 §6's `validation_error` details example is one flat field/issue pair, not DRF's default per-field list-of-messages aggregation. Because it isn't `rest_framework.exceptions.ValidationError`, DRF's `is_valid()` doesn't intercept it — it propagates straight to `kairos_exception_handler`, which builds the exact shape | `kairos/core/exceptions.py`, `kairos/bookings/serializers.py` |
| `REST_FRAMEWORK["UNAUTHENTICATED_USER"]` is explicitly `None` | DRF's default is the string path to `django.contrib.auth.models.AnonymousUser` — importing that module pulls in `ContentType`, which fails because `contenttypes` isn't installed (Phase 2's deliberate decision). `None` makes DRF leave `request.user` as `None` for anonymous requests instead, which `IsAuthenticated` already handles correctly without needing `contenttypes` at all | `kairos/settings/base.py` |
| `StubUserIdAuthentication.authenticate_header()` returns `"X-Dev-User-Id"` instead of the `BaseAuthentication` default of `None` | Without a `WWW-Authenticate` challenge available, DRF's `APIView.handle_exception()` silently downgrades `NotAuthenticated` from 401 to 403 (HTTP requires a challenge header alongside a bare 401). Spec v1.0 §5.1 documents 401 specifically for `unauthorized` | `kairos/identity/authentication.py` |
| `BookingService.create_booking` calls `booking.refresh_from_db()` immediately after `Booking.objects.create(...)` | `.create()` leaves fields exactly as assigned in Python — `time_range` stays the plain tuple passed in, not the `Range` object a fresh `SELECT` returns, which broke response serialization (`AttributeError: 'tuple' object has no attribute 'lower'`) until this was added. Also correctly picks up the generated `starts_at` column and the DB-stored `created_at` precision | `kairos/bookings/services.py` |
| CONC-01/02/05's `MAX_ROUND_ATTEMPTS` raised 3 → 6 (Phase 4), then 6 → 10 (Phase 6) | Phase 4's "confirm all CONC tests still pass" check caught real flakiness: at N=200 the per-attempt zero-success rate is ~15-20%, so 3 consecutive zero-success attempts (retry budget exhausted) happened in a live run — a ~5% chance of flaking any given 10-run suite at the old value; 6 pushed that below ~0.1% *assuming independent attempts*. Phase 6's own "confirm all prior tests still pass" check then directly observed a genuine 6-in-a-row exhaustion when CONC-01 ran immediately after ~55 other tests in the same session (including Phase 5's IDEM-06 with its own 100 threaded requests) — re-running in isolation immediately after showed the ordinary ~15% rate. This means the failures are somewhat CORRELATED under sustained system load, not the cleanly independent trials the original estimate assumed; 10 adds headroom against that. **Relevant to Phase 28**: the full 100-run CONC-01 exercise multiplies whichever risk remains by 10×, and a real CI runner's load profile may differ from this dev machine's — revisit this budget (or the underlying timeout tuning RFC v1.0 §18 already flags) before that phase, not after it flakes | `tests/concurrency/test_conc_01.py`, `test_conc_02.py`, `test_conc_05.py` |
| `NotFoundError` (renamed from Phase 4's `ResourceNotFoundError`) is used for every 404 `not_found` case — missing/inactive resource, missing booking, and a booking the requester can't view | Phase 6 added booking-not-found and view-permission-denied cases that map to the identical 404 `not_found` code Phase 4's resource-lookup already used. Reusing one exception (renamed to drop the resource-specific name) avoids two exception classes with identical HTTP behavior | `kairos/core/exceptions.py` |
| A non-owner, non-admin `GET /bookings/{id}` returns 404, not 403, exactly like a nonexistent booking | Spec v1.0 §1's convention: object-level protection is 404 so a caller can't distinguish "doesn't exist" from "exists but isn't yours" — unlike `resource_id`-scoped `GET /bookings` (403), where the resource's existence is already known/browsable, so only the *action* is gated | `kairos/bookings/views.py` (`BookingDetailView`) |
| `KairosAPIView` (shared base class in `core`) declares `authentication_classes`/`permission_classes` once | By Phase 6 there are five view classes needing the identical stub-auth + IsAuthenticated configuration Phase 4 first wrote inline on `BookingCreateView` — worth extracting once real duplication exists, not before | `kairos/core/views.py` |
| Availability's `booking_id`/`owner` reveal check (`is_resource_admin`/`is_operations`) is computed ONCE per request, outside the per-booking loop | The N+1 guard RFC v1.0 §7.2 asks for: whether a field is revealed depends only on (requester, resource), never on which specific booking, so computing it once and reusing it for every busy block keeps the query count constant regardless of how many bookings are in range | `kairos/resources/views.py` (`ResourceAvailabilityView`) |
| Held slots omit `booking_id`/`owner` unconditionally in the availability view — even from a resource admin | Spec v1.0 §5.7: exposing which booking a hold corresponds to would leak waitlist queue state to anyone, admin included. This is a separate rule from the ownership-based omission, and stricter — no privilege level reveals a hold's identity through this endpoint | `kairos/resources/views.py` |
| Cursor pagination on `GET /bookings` and `GET /resources` uses keyset filtering (`Q(sort_key__gt=...) \| Q(sort_key=..., id__gt=...)`), never `OFFSET` | Spec v1.0 §8: an offset shifts under concurrent inserts/deletes, silently skipping or duplicating rows — every list endpoint here is concurrently written. Proven directly: a test inserts a row between two page fetches and asserts no skips or duplicates | `kairos/core/pagination.py`, `tests/bookings/test_read_endpoints.py` |
| `idempotency_key`'s PK uses Django 6.1's `models.CompositePrimaryKey("user_id", "key")` — a genuine composite PK, not the surrogate-key-plus-`UniqueConstraint` workaround `resource_admin` needed in Phase 2 | Phase 5's DoD explicitly verifies the PK via `\d idempotency_key`; `CompositePrimaryKey` (new since Django 5.2) makes this a real composite PK now rather than requiring a workaround. Confirmed via `psql \d`: `idempotency_key_pkey PRIMARY KEY, btree (user_id, key)` | `kairos/core/models.py` |
| Write-path session settings are applied ONCE at the top of `run_idempotent_write`'s outer transaction, before the key-claim INSERT — not left to `BookingService`'s own (redundant, too-late) internal call | The key-claim INSERT is now the FIRST statement in the transaction (Spec v1.0 §4.1's literal ordering) — if `lock_timeout` etc. were only applied inside the nested `create_booking()` call, a concurrent replay's key-claim insert would block using Postgres's default (no timeout) instead of the intended 3s budget, undermining IDEM-06's request_in_progress path entirely. Extracted the shared helper into `kairos/core/db.py` (RFC §4.1's "db helpers," reserved since Phase 2) so both `BookingService` and the idempotency wrapper call the identical function — the nested call inside `create_booking()` re-applies the same values harmlessly | `kairos/core/db.py`, `kairos/core/idempotency.py` |
| A 409 `slot_unavailable` idempotency outcome is recorded in its OWN, separate transaction after the write's transaction rolls back — never inside the same transaction as the failed write | RFC v1.0 §11.2 states this explicitly ("in its own transaction after the rollback"). The failed write's rollback undoes the ENTIRE transaction, including the key claim — there is nothing left to `UPDATE`, so the 409 outcome must be a fresh `INSERT` in a new transaction. A 503 outcome is the opposite: nothing is recorded at all, since the write's outcome is genuinely unknown (Spec v1.0 §5.1) and a retry with the same key should start completely fresh, not receive a stale "unknown" result | `kairos/core/idempotency.py` (`run_idempotent_write`, `_record_conflict_outcome`) |
| Policy validation (bookable hours, duration, past-dating, horizon) happens BEFORE the idempotency key is ever claimed, not inside the protected transaction | Spec v1.0 §7 point 7 ("conflict outcomes are recorded too") is scoped to 409 specifically, not general validation failures — and a malformed/policy-violating request has nothing worth protecting or replaying. Validating first also means a request that will never succeed doesn't consume a key slot | `kairos/bookings/views.py` (`BookingCreateView.post`) |
| `BookingResponseSerializer.start`/`.end` changed from `SerializerMethodField` (returning a raw `datetime`) to `DateTimeField(source="time_range.lower"/".upper")` | A raw datetime returned by `SerializerMethodField` gets formatted differently by two different code paths: Django's `DjangoJSONEncoder` (used when storing the response into `IdempotencyKey.response_body`, a JSONField) truncates microseconds to milliseconds, while DRF's own response renderer preserves full microsecond precision — producing two different strings for the identical instant and breaking IDEM-02's "identical stored response returned verbatim." Formatting to a string once, through the same `DateTimeField` code path `created_at` already used, fixed it | `kairos/bookings/serializers.py` |
| Cancel's conditional UPDATE is guarded on the booking's CURRENT status (`WHERE status='confirmed'`), not the target one, and the affected row-count decides whether the row was actually flipped this call | Spec v1.0 §5.6: cancelling an already-cancelled booking must return 200 with the existing state, not an error — the guard makes the UPDATE match zero rows in that case instead of raising or double-applying, and the row-count (not a re-read-then-compare) is what decides whether the `on_commit` waitlist-check hook fires, since a no-op cancel has nothing to notify anyone about | `kairos/bookings/services.py` (`cancel_booking`) |
| Idempotency fingerprints for `PATCH /bookings/{id}` and `POST /bookings/{id}/cancel` fold the URL's `booking_id` into the body passed to `run_idempotent_write`, not just the `endpoint` label | `compute_request_fingerprint()` hashes only the body (Phase 5). An edit/cancel body — `{"start","end"}` or `{"reason"}` — never mentions which booking it's about; without folding the id in, reusing one idempotency key across two DIFFERENT bookings with a coincidentally identical body would be misread as a replay of the first, silently never touching the second. Phase 4/5's create doesn't have this problem because `resource_id` is already part of its body. Folding the id in surfaces the reuse as the same 422 `idempotency_key_conflict` any other same-key-different-body reuse gets (IDEM-03), rather than a silent wrong-booking replay | `kairos/bookings/views.py` (`BookingDetailView.patch`, `BookingCancelView.post`) |
| `_handle_write_database_error` (shared by create/edit/cancel) is typed to return `NoReturn` and is used uniformly across all three, even though cancel's UPDATE can never actually trigger SQLSTATE 23P01 | A partial EXCLUDE constraint only fires on rows satisfying its predicate (`status IN ('confirmed','held')`); cancel's UPDATE moves a row OUT of that set, so Postgres never evaluates the constraint against it. The branch is unreachable for cancel specifically, but keeping one shared function — rather than a cancel-specific subset — is what RFC v1.0 §17 asks for ("every future write path... gets consistent SQLSTATE translation... for free"), and an unreachable branch costs nothing | `kairos/bookings/services.py` |
| `BookingResponseSerializer` gained `cancelled_at`/`cancelled_by`/`cancellation_reason` as one shared extension, not a cancel-only response variant | Spec v1.0 §5.2 already promises GET's shape matches §5.1's exactly regardless of booking status, and a cancelled booking now genuinely exists post-Phase-7 — a GET that omitted why/when it was cancelled would be a real product gap, not just an unused field. One serializer keeps every endpoint (create, GET, edit, cancel) returning the identical shape rather than diverging per endpoint | `kairos/bookings/serializers.py` (`BookingResponseSerializer`) |
| ⚠️ **Regression found and fixed in Phase 7**: `run_idempotent_write`'s key-claim INSERT was NOT calling `apply_write_path_session_settings` before it ran, despite this file's own Phase 5 entry claiming the fix was "moved into a shared `core/db.py` helper applied once at the top of the outer transaction" | Verified directly: `kairos/core/idempotency.py` had no cursor/session-settings call at all — the Phase 5 report described the intended fix, but it was never actually wired into `run_idempotent_write` (only each write function's own NESTED transaction called it, which runs strictly AFTER the key-claim INSERT has already executed under Postgres's untimed defaults). Confirmed empirically before fixing: a spy on the key-claim `.create()` call observed `SHOW lock_timeout` = `'0'` (no timeout) at that exact statement. Fixed by adding the same `apply_write_path_session_settings(cursor, ...)` call at the top of `run_idempotent_write`'s outer `transaction.atomic()`, before the key-claim INSERT — verified the same spy now observes `3s`/`10s`, and that reverting the fix makes the new test fail (confirming the test is a real regression guard, not incidental). IDEM-06 (100 concurrent-replay reps) and all five CONC tests still pass | `kairos/core/idempotency.py`, `tests/bookings/test_idempotency.py` (`test_session_settings_are_active_at_the_key_claim_insert_itself`) |
| `app.actor_type`/`app.reason` (Phase 8) are applied through the SAME `apply_write_path_session_settings` call as the write-path timeouts and `app.actor_id`/`app.request_id` — not a second, separate context manager for "audit settings" | Explicit instruction after Phase 7's session-settings regression: two categories of `SET LOCAL`-equivalent value sharing one mechanism and one call site is what makes a repeat of that exact regression structurally harder — a second mechanism would need its own correctness proof and could regress independently. Extended (not duplicated) Phase 7's own regression test to assert BOTH categories are visible at the key-claim INSERT together | `kairos/core/db.py` (`apply_write_path_session_settings`), `tests/bookings/test_idempotency.py` |
| `_compute_changes()` diffs EVERY field in an audit row's before/after JSONB snapshots, not just `status` | Spec v1.0 §5.3's example `changes` bodies both happen to show only a `status` transition (create, admin-cancel) — but Phase 7's edit changes `time_range` while leaving `status` completely untouched. A narrower "just show status changes" reading would make an edit's history entry show NOTHING, failing AUD-04's actual requirement (full lifecycle reconstruction) | `kairos/bookings/views.py` (`BookingHistoryView`, `_compute_changes`) |
| ⚠️ **Bug caught by AUD-02 itself, before merge**: `AuditLog.occurred_at` used Django's `auto_now_add=True`, which is Python-side only — the trigger's raw `INSERT INTO audit_log` (no ORM involved) hit a NOT NULL violation the first time a write bypassed Django entirely | Spec v1.0 §3's DDL declares `occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()` — a genuine column-level default, which `auto_now_add` does not create. Fixed with Django 5+'s `db_default=Now()`, which does. Reproduced with a raw SQL insert into `resource` before the fix (failed), confirmed the identical insert succeeds after it | `kairos/core/models.py` (`AuditLog.occurred_at`), `kairos/core/migrations/0002_auditlog.py` |
| ⚠️ **Bug caught by the test suite, before merge**: `resource_admin`'s surrogate `id` was Django's implicit `BigAutoField` (bigint) — the audit trigger's `COALESCE(NEW.id, OLD.id)` into `audit_log.entity_id UUID` failed with a type mismatch the first time a test wrote a `ResourceAdmin` row after the trigger was attached | Every other entity table (`app_user`/`resource`/`booking`) declares an explicit `UUIDField` PK; `resource_admin` was the one exception, an oversight from Phase 2 rather than a deliberate choice. Fixed via a hand-written `RunSQL` migration (Django's auto-generated `AlterField` SQL assumes a bigint→uuid CAST exists, which Postgres doesn't have — confirmed empirically, `cannot cast type bigint to uuid`) with `state_operations` keeping Django's migration state in sync. Safe only because `resource_admin` carries no production data yet (Phase 19 is the first phase to write it via a real endpoint) | `kairos/identity/models.py` (`ResourceAdmin.id`), `kairos/identity/migrations/0003_alter_resourceadmin_id.py` |
| The RUNNING APPLICATION's default `DATABASE_URL` now points at `kairos_app` (least-privilege), not the `kairos` superuser docker-compose provisions — `manage.py migrate` requires a temporary override to the superuser DSN | AUD-01's entire premise — that the app role literally CANNOT violate the append-only guarantee — is only true if the app actually connects as that role, not merely if the role exists. Caught mid-phase: `manage.py runserver` under the new default crashed at startup (`permission denied for table django_migrations`) because every management command's `check_migrations()` queries that table — kairos_app needed an explicit `SELECT` grant on Django's own bookkeeping table, not just the application tables, before the app could even start. Verified live: full create→edit→cancel→history round trip over real HTTP with the dev server running as `kairos_app` (`SELECT current_user` confirmed) | `kairos/settings/base.py`, `.env.example`, `kairos/core/migrations/0003_audit_trail_triggers_and_grants.py` |

## Running Locally

```bash
cd infra
docker compose up -d
docker exec kairos_postgres psql -U kairos -d kairos_dev -c "SELECT 1;"

cd ../backend
python -m venv .venv
.venv/Scripts/activate      # Windows; `source .venv/bin/activate` on macOS/Linux
pip install -e ".[dev]"

# Migrations need DDL privileges the app's own kairos_app role deliberately
# doesn't have (Phase 8) — override DATABASE_URL to the superuser DSN for
# this ONE command only:
DATABASE_URL=postgresql://kairos:kairos@localhost:5432/kairos_dev python manage.py migrate

python manage.py runserver  # now defaults to kairos_app — POST /api/v1/bookings is live,
                             # see README.md for a curl example
```

The frontend starts Phase 23.

## Running Tests

```bash
cd backend
pytest tests/concurrency -v   # Milestone 1 — the project's central proof, run this first
pytest                        # full suite: the above + smoke test + schema assertion
```

`tests/concurrency/` — CONC-01 (N=200 identical-slot, 10 runs), CONC-02 (partial + 5-way
chained overlap, 10 runs each), CONC-03 (edit-vs-create race, Phase 7, 10 runs), CONC-04
(edit-vs-edit race, Phase 7, 10 runs), CONC-05 (cancel-and-rebook race, 10 runs). Each round
is retried up to 10 times only if it produced zero successes (a documented, load-correlated
liveness characteristic — see Key Technical Decisions); more than one success on any single
attempt fails immediately and is never retried. CONC-03/04 exercise the raw UPDATE/INSERT
SQL directly through the same barrier-released harness as the others, not through the
service/view layer — proving the constraint itself, independent of Phase 7's application
code. `tests/test_schema_assertion.py` (RECON-05 CI form) fails the moment
`no_overlapping_bookings`'s predicate is narrowed — verified by hand during Phase 3 (narrowed
it, watched the test fail, reverted). `tests/test_audit_trail.py` (Phase 8) — AUD-01
(connecting AS `kairos_app` via a dedicated psycopg connection, not the test session's own
DB role, and watching `UPDATE`/`DELETE` on `audit_log` fail with `InsufficientPrivilege`; a
direct grant-catalog inspection too, so a future migration can't silently widen them
unnoticed), AUD-02 (a raw SQL write to `booking` still produces an audit row; all three
Phase 8 triggers exist), and the `actor_type='system'` attribution mechanism (no real worker
exists yet to exercise this — Phase 16 — so the trigger's handling of that session variable
is proven directly). `tests/bookings/` covers `BookingService` (session-settings assertion,
all four SQLSTATE translations — 55P03 forced genuinely via a real held row, 40P01/57014
forced by simulation since natural reproduction isn't controllable on demand), the write API
(every Test Plan §10 policy-validation row, 409, 404, 401, `X-Request-Id`), idempotency
(`test_idempotency.py`) — IDEM-01–04, 06 (100 barrier-released repetitions), 09, 10, 11, the
composite-PK schema check, the cleanup command, and (Phase 7, extended in Phase 8)
`test_session_settings_are_active_at_the_key_claim_insert_itself` — a spy on the key-claim
INSERT itself proving `lock_timeout`/`statement_timeout` AND `app.actor_type`/`app.reason`
are all active together at that exact statement, added after finding `run_idempotent_write`
had silently regressed on the Phase 5 fix it claimed to have (see Key Technical Decisions) —
the read path (`test_read_endpoints.py`, Phase 6): detail/list authorization, held-row
exclusion, and cursor-pagination stability under a concurrent insert; cancel/edit
(`test_cancel_edit.py`, Phase 7): every Spec §5.5/§5.6 failure case from the Test Plan §10
matrix, double-cancel idempotence, self-conflict-on-edit, and the two same-key-different-
booking tests proving the idempotency-fingerprint gap described in Key Technical Decisions is
actually closed (422 conflict, not a silent wrong-booking replay); and (Phase 8)
`test_history.py` — AUD-03(a)/(b) (actor attribution and reason through the real
create/admin-cancel API, correlated to the same `X-Request-Id`), AUD-04 (create→edit→
admin-cancel reconstructs in order via `GET /bookings/{id}/history`, including a genuine
field-level diff — not just status transitions — proving the edit event actually shows what
changed), and AUD-05 (cancellation doesn't remove history). `tests/resources/` (Phase 6)
covers resource list/detail and availability — the 92/93-day boundary, SEC-05's key-absence
assertion, held-slot opacity even to admins, and the bounded query-count guard.
Also runnable: `cd backend && ruff check . && ruff format --check . && mypy kairos` (all pass
with zero findings as of Phase 8). CI (`.github/workflows/ci.yml`) runs all of this as three
jobs — `lint`, `test`, `concurrency` — on every PR. The spike scripts under `scripts/spike/`
are runnable but are diagnostic, not a test suite — see
`docs/spikes/S1-postgres-verification.md` for what each one does and its recorded output.

## Open Questions

None from Phase 0. From Phase 1's spike:

- **S1.1 on the real deployment target is still unverified.** No deployment platform has
  been chosen yet. Only local Docker PostgreSQL 16 has been confirmed. Must be re-checked
  once a platform is chosen, and again before Phase 30 go-live (Rollout v1.0 §2.2).
- **S1.6's throughput numbers are a local, connection-overhead-dominated baseline**, not a
  production ceiling — Phase 29 (Test Plan CONC-06) re-measures this against a pooled,
  production-shaped topology.

Genuine open questions from the source documents (offer window duration, nonexistent-time
policy default, series bounds, etc.) are tracked in PRD v1.0 §11 and RFC v1.0 §18; they get
resolved or explicitly deferred as the relevant phases are built.
