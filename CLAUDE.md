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
│   │   │                   # timezones.py (validate_iana_zone, local_to_instant,
│   │   │                   # is_nonexistent_local_time/is_ambiguous_local_time,
│   │   │                   # tzdata_version — Phase 10, consumed by Phase 11's recurrence
│   │   │                   # engine), apps.py (CoreConfig.ready() logs tzdata_version at
│   │   │                   # startup — Phase 10),
│   │   │                   # management/commands/cleanup_idempotency_keys.py
│   │   ├── identity/       # app_user, resource_admin (UUID surrogate PK since Phase 8),
│   │   │                   # user_group/user_group_membership (Phase 9 — PRD FR46, not in
│   │   │                   # Spec v1.0 §3 at all, see Key Technical Decisions),
│   │   │                   # authentication.py (OIDCSessionAuthentication — real; Stub
│   │   │                   # UserIdAuthentication — gated to test only, Phase 9),
│   │   │                   # authorization.py (AuthorizationService, Phase 9 — the ONE
│   │   │                   # place every permission decision is resolved),
│   │   │                   # oidc.py (JWT mint/verify + local mock issuer, Phase 9),
│   │   │                   # views.py/urls.py (POST /auth/token, /auth/dev-mock-login)
│   │   ├── resources/      # resource (+ restricted_group FK, Phase 9; timezone validated as
│   │   │                   # IANA in Resource.save() — Phase 10), serializers.py,
│   │   │                   # views.py, urls.py (list/detail/availability — Phase 6; writes
│   │   │                   # are Phase 19)
│   │   ├── bookings/       # booking, services.py (create/edit/cancel — Phase 7), serializers.py,
│   │   │                   # views.py (BookingHistoryView — Phase 8), urls.py
│   │   ├── urls.py, wsgi.py
│   ├── tests/
│   │   ├── test_booking_exclusion_smoke.py
│   │   ├── test_schema_assertion.py   # RECON-05 CI form — fails if the predicate is narrowed
│   │   ├── test_audit_trail.py        # AUD-01, AUD-02, grant/trigger-existence checks (Phase 8)
│   │   ├── test_security.py           # SEC-01, SEC-06 (Phase 9)
│   │   ├── test_timezones.py          # TZ-02, TZ-04, TZ-03 Test A, IANA validation,
│   │   │                              # nonexistent/ambiguous detection (Phase 10)
│   │   ├── conftest.py                # app_user / active_resource fixtures, shared
│   │   ├── identity/                  # test_authentication.py (real OIDC flow, actor_id spy,
│   │   │                              # dev-settings-subprocess X-Dev-User-Id rejection),
│   │   │                              # test_authorization.py (four roles, scoped admin) — Phase 9
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
│   └── pyproject.toml      # ruff, mypy strict, pytest config; pyjwt[crypto] added Phase 9;
│                           # tzdata pinned exactly (==) added Phase 10
├── frontend/              # React + TypeScript — empty until Phase 23
├── infra/                 # docker-compose.yml, init-test-db.sql
└── scripts/
    └── spike/             # throwaway S1 spike scripts — will NOT be extended after Phase 1
```

Endpoints live (Phase 6 added the read path, Phase 7 the remaining single-booking mutations,
Phase 8 the history endpoint, Phase 9 real auth, to Phase 4/5's write path): `POST`/
`GET /api/v1/bookings`, `GET`/`PATCH /api/v1/bookings/{id}`, `POST /api/v1/bookings/{id}/cancel`,
`GET /api/v1/bookings/{id}/history`, `GET /api/v1/resources`, `GET /api/v1/resources/{id}`,
`GET /api/v1/resources/{id}/availability`, `POST /api/v1/auth/token`,
`POST /api/v1/auth/dev-mock-login` (dev/test only). Every mutation (create, edit, cancel) is
idempotent (Phase 5/7 — `Idempotency-Key` is required; missing it is 400). Edit is owner-only,
no admin override; cancel is owner-or-scoped-admin, with a reason required for the
admin-override case (400 otherwise). Cancelling an already-cancelled booking is a 200 no-op,
independent of idempotency key.

Real authentication (Phase 9, RFC v1.0 §4): `Authorization: Bearer <session-token>`, validated
by `OIDCSessionAuthentication`. A client obtains that session token via `POST /auth/token`
with a verified OIDC ID token — in dev/test, `POST /auth/dev-mock-login` mints one against a
fixed local RS256 keypair standing in for a real IdP (no Keycloak or other external dependency
required); in prod, a real provider's JWKS-published key verifies it (structurally complete,
genuinely untested against a live IdP — same documented-gap pattern as IDEM-07/08). The
`X-Dev-User-Id` stub (Phase 4) still exists but is now inert everywhere except
`kairos.settings.test` — gated by `settings.KAIROS_DEV_AUTH_STUB_ENABLED`, checked at request
time, verified by actually starting the app under `kairos.settings.dev` in a real subprocess
and confirming a real HTTP request carrying that header gets a bare 401. Four roles (PRD
FR44) — `booker`, `resource_administrator` (scoped via `resource_admin`), `system_admin`
(global), `operations` (read-only) — are resolved through exactly one place,
`AuthorizationService`, consulted by every view instead of each view re-deriving permission
logic inline. Resources can now be restricted to a `user_group` (PRD FR46) — absent from list
results and 404 on direct access for non-members, exactly like a nonexistent resource
(SEC-06).

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
| 8 | Audit Trail — Triggers & Grants ⚠️ Subtle | `audit_log` table + `write_audit_log()` trigger on `booking`/`resource`/`resource_admin`, firing unconditionally on every INSERT/UPDATE/DELETE — proven by a raw SQL write that never touches the service layer (AUD-02); a dedicated `kairos_app` database role holds ordinary DML on every app table but only `INSERT`/`SELECT` (never `UPDATE`/`DELETE`) on `audit_log`, enforced at the grant level and proven by actually connecting AS that role (AUD-01) — the RUNNING APPLICATION now connects as `kairos_app`, not the superuser, verified live via `manage.py runserver` and a full create→edit→cancel→history round trip over real HTTP; `app.actor_type`/`app.reason` propagate through the SAME shared `apply_write_path_session_settings` call as the write-path timeouts (not a second context manager), per explicit instruction after Phase 7's regression — verified by extending that exact regression test, not adding a parallel one; `GET /bookings/{id}/history` reconstructs full lifecycles via a genuine before/after field-level diff (`_compute_changes`), not just status transitions, since Phase 7's edit changes `time_range` while leaving `status` untouched — AUD-03(a)(b), AUD-04, AUD-05 all pass; AUD-03(d)'s "system-initiated write" has no real worker yet (Phase 16), so the underlying mechanism is proven directly instead. Three real bugs found and fixed via hands-on verification, not just passing tests: (1) `occurred_at` used `auto_now_add` (Python-side only) instead of a genuine `db_default`, so the trigger's raw INSERT — which never goes through Django's ORM — hit a NOT NULL violation; (2) `resource_admin`'s implicit `BigAutoField` surrogate PK couldn't satisfy the trigger's `COALESCE(NEW.id, OLD.id)` into `audit_log.entity_id UUID`, so it's now an explicit UUID PK like every other entity table; (3) `kairos_app` had no grant on Django's own `django_migrations` table, so the app failed to even START under the new role until caught by actually running `manage.py runserver`, not only the test suite. Full suite (96 tests) green | Merged (PR #8) |
| 9 | Authentication & Scoped Authorization | `OIDCSessionAuthentication` validates `Authorization: Bearer <session-token>`, issued by new `POST /api/v1/auth/token` after verifying a real (RS256, JWKS) or — dev/test only — mock OIDC ID token from `POST /api/v1/auth/dev-mock-login`, signed against a fixed local keypair instead of requiring Keycloak or any other external dependency; the backend's own session token is a SEPARATE, short-lived HS256 token (RFC v1.0 §4), not the raw ID token. `AuthorizationService` (`kairos/identity/authorization.py`) is now the ONE place every permission decision is resolved — every prior inline `is_resource_admin(...) or is_operations(...)` check in `bookings/views.py` and `resources/views.py` replaced with a call into it; PRD FR44's four roles (booker/resource_administrator/system_admin/operations) and PRD FR45's scoped-admin isolation (an admin for Resource A structurally cannot administer Resource B — `can_administer_resource` always re-checks against the specific resource, tested explicitly through the real cancel endpoint) are enforced through it uniformly. `X-Dev-User-Id` (Phase 4) is now inert outside `kairos.settings.test`, gated by `settings.KAIROS_DEV_AUTH_STUB_ENABLED` checked at request time — NOT verified by inspection alone, per explicit instruction: a dedicated test actually starts the app under `kairos.settings.dev` in a real subprocess and confirms a real HTTP request carrying that header gets a bare 401 (`WWW-Authenticate: Bearer`, not the stub's own challenge), independently reproduced live via `curl` against a real dev-mode server too. `app.actor_id` reaching the key-claim INSERT under a REAL authenticated principal (not a stub) is proven the same spy-on-cursor way Phase 7/8 proved the timeout/actor-type settings — reusing the identical `apply_write_path_session_settings` call site, per explicit instruction not to introduce a second mechanism for it. PRD FR46's "restricted resources" required inventing schema Spec v1.0 §3 never defined (`user_group`/`user_group_membership`, `resource.restricted_group`) — see Key Technical Decisions for the scoping call. SEC-01 (IDOR + response-body leakage across GET/PATCH/cancel/history) and SEC-06 (restricted resource 404 + absent from list, including the booking-creation and availability paths, not just resource detail) both pass. Post-review revision (caught by re-reading the DoD literally, not by a new test failing): 8 representative existing tests — create (full mock-login→token-exchange round trip), create-conflict-409, edit, self-cancel, admin-override-cancel, IDEM-01/02, and history's AUD-03(a) — converted to real minted session tokens, proving the write path (session settings, audit attribution, idempotency scoping) actually works end-to-end under real identity, not just that the auth layer and the existing suite each work in isolation; the remaining ~85 tests keep the stub deliberately (gated to `kairos.settings.test` only), and CONC-01–05 aren't candidates at all — no HTTP/auth layer exists in them to convert (raw psycopg SQL by design). Three real bugs found and fixed via hands-on verification: (1) `KAIROS_SESSION_SIGNING_KEY`'s fallback chain (env var → `SECRET_KEY`) produced an empty HMAC key, since `SECRET_KEY` is itself commonly empty in dev/test — PyJWT refused to sign, caught by the first real login attempt; (2) both new unauthenticated auth views' `authentication_classes = []` triggered the SAME 401→403 DRF downgrade this codebase already documents for `StubUserIdAuthentication` (no authenticator means no `WWW-Authenticate` challenge); (3) `can_administer_resource` was a strictly broader check than the pre-Phase-9 inline permission logic it replaced (now also recognizes `system_admin`, which those checks never consulted) — a genuine pre-existing gap the consolidation surfaced, not a deliberate feature. Full suite (121 tests — the 8 conversions modified existing tests rather than adding new ones) green | Pending (on branch `phase-09-auth-scoped-authz`) |
| 10 | Timezone Foundation | `USE_TZ=True`/`TIME_ZONE='UTC'` confirmed already correct since Phase 2 — no change needed. New `kairos/core/timezones.py` is now the ONE place every IANA-zone check and local→UTC conversion goes through: `validate_iana_zone` (membership in `zoneinfo.available_timezones()`, so a fixed offset like `+01:00` is rejected — PRD FR8), `local_to_instant(local_dt, zone, on_date)` (combines `on_date` with `local_dt`'s wall-clock time and localizes using the rules in effect on `on_date` SPECIFICALLY — `on_date` is authoritative, never whatever date `local_dt` itself carries, which is what makes the RFC §9.1 creation-vs-occurrence bug structurally impossible to reintroduce here), `is_nonexistent_local_time`/`is_ambiguous_local_time` (round-trip and `fold`-based detection per RFC §9.3, unit-tested against the exact Europe/Paris 2027-03-28/2027-10-31 dates Test Plan TZ-05/TZ-06 use — built now, consumed by Phase 11), and `tzdata_version()`. `tzdata` is pinned EXACTLY (`==2026.3`, not a range) in `pyproject.toml` — required cross-platform since Windows and many minimal Linux images ship no system IANA database at all for `zoneinfo` to fall back on; its version is logged via the existing structured JSON logger on every app startup (`CoreConfig.ready()`, verified live via `manage.py check`) and a CI-form test (`tests/test_timezones.py`) asserts the pin is exact and the installed version matches it (Test Plan TZ-03 Test A). `Resource.save()` now calls `validate_iana_zone` unconditionally, so the only live write path today (direct ORM — Phase 19 adds a real endpoint) already cannot bypass it; raises the existing framework-agnostic `PolicyValidationError`, not Django's own `ValidationError`, so Phase 19's future serializer needs zero adaptation to turn it into 400 `validation_error`. TZ-02 passes as a direct unit test of `local_to_instant` (Oct-20-creation/Nov-10-occurrence resolves to `2026-11-10T15:00:00Z`, EST — not the `14:00:00Z` EDT bug); TZ-04 passes as a real HTTP test hitting `GET /resources/{id}/availability` as two different authenticated users and asserting byte-identical UTC `busy_blocks` — there is no per-viewer localization concept anywhere in the backend to produce a difference. A genuine spec gap surfaced, not fixed: PRD FR7's second sentence ("store the IANA timezone identifier under which [a one-off booking] was created, for display and audit") has no corresponding `booking` column in Spec v1.0 §3 at all, and this phase's own Scope IN / DoD (unlike its "Documents satisfied" line) never actually calls for adding one — flagged rather than silently built or silently dropped, see Key Technical Decisions and Open Questions. Full suite (134 tests — 121 prior + 13 new) green, including all five CONC tests | Pending (on branch `phase-10-timezone-foundation`) |

## Current Phase In Progress

None. Phase 10 is complete pending review and merge. Phase 11 (Recurrence Materialization & DST) is next.

## NOT Yet Built

No Celery/Redis, no frontend, no hold reclamation (booking creation does not yet run the
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
writes `resource_admin` yet (`ResourceAdmin` rows in tests are created directly via the ORM,
not through any endpoint). IDEM-05 (recurring replay) needs Phase 12's endpoint; IDEM-07/08
(fault injection — process kill mid-transaction, proxy-level response drop) need tooling that
arrives in Phase 28; idempotency coverage on waitlist join/offer confirm arrives with those
endpoints (Phases 14, 16). `booking.series_id` does not exist yet — it cannot, since
`recurring_series` (Phase 11) doesn't exist; it is added in Phase 11, not retrofitted early.
No `booking` column records the IANA zone a one-off booking was created under (PRD FR7's
second sentence) — Spec v1.0 §3 never defined one, and Phase 10 flagged rather than invented
one (see Key Technical Decisions/Open Questions); whichever future phase needs it for
display/audit should add it deliberately rather than assume it already exists. Recurrence
expansion (`kairos/bookings/recurrence.py`, `recurring_series` table, nonexistent/ambiguous
POLICY APPLICATION as opposed to Phase 10's detection-only utilities) is entirely Phase 11 —
`kairos/core/timezones.py`'s `local_to_instant`/`is_nonexistent_local_time`/
`is_ambiguous_local_time` exist now specifically so Phase 11 doesn't have to build them under
time pressure alongside the DST expansion logic itself.
`kairos_app`'s password (Phase 8) and `KAIROS_SESSION_SIGNING_KEY`'s dev-only fallback (Phase
9) are both hardcoded dev-only literals, matching `infra/docker-compose.yml`'s own
precedent — Rollout (Phase 30) must replace both with real managed secrets before any
deployment; `prod.py` already refuses to start with either the empty-`SECRET_KEY` case or the
literal signing-key fallback in play, so this is enforced, not just documented. Real OIDC
(RS256 JWKS-based token verification, `kairos/identity/oidc.py`'s `_fetch_jwks_public_key`) is
structurally complete but genuinely UNTESTED against a live IdP — this project has none to
test against, the same documented-gap pattern as IDEM-07/08; only the local mock-issuer path
is exercised end-to-end. User-group MANAGEMENT (creating groups, adding/removing members) has
no endpoint yet — Phase 9 built the schema and the enforcement (`AuthorizationService`,
SEC-06) `user_group`/`user_group_membership` rows are ORM-created in tests, same caveat as
`resource_admin` above; whichever future phase owns admin-facing resource/group management
should wire this up rather than leaving it ORM-only indefinitely. Only 8 representative
existing tests were converted to real minted session tokens (see Key Technical Decisions for
which, and why those specifically) — the remaining ~85 still authenticate via the gated
`X-Dev-User-Id` stub, by design only reachable under `kairos.settings.test`; group-management
endpoints landing in a later phase (see the Open Questions entry from this phase) are one more
reason not every test needs converting now. CONC-01's full 100-run + N=500
escalation and CONC-06 (throughput characterization) are deferred to Phase 28/29 respectively;
CI only runs the 10-run CI-tier reduction for every CONC test. The throwaway spike table
`spike_booking` may still exist in `kairos_dev`, created/dropped repeatedly by
`scripts/spike/common.py` — unrelated to the real
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
| The local mock OIDC provider (Phase 9) is a fixed RS256 keypair + two small view endpoints, not a real Keycloak/IdP running in Docker | The Implementation Plan phase text explicitly names both options ("Keycloak in Docker Compose, or a stub issuer"). A stub issuer keeps "the system runs without external dependencies" true while still exercising REAL signature/issuer/audience/expiry verification — a forged token signed with a different keypair is rejected exactly like it would be against a real IdP (proven directly: `test_token_exchange_rejects_wrong_signature`). Only the ISSUER is fake; the verification code path is the same one a real provider's tokens go through | `kairos/identity/oidc.py` |
| The backend's own session token is a SEPARATE, HS256-signed JWT — never the raw OIDC ID token forwarded as-is | RFC v1.0 §4 says the backend "issues its own short-lived internal session token," not that it re-uses the IdP's. Re-validating a full RS256 token (or worse, calling out to the IdP) on every single API request would be unnecessary latency and an unnecessary external dependency per request; an HS256 token this service both signs and verifies is cheaper and needs no network call | `kairos/identity/oidc.py` (`issue_session_token`/`verify_session_token`) |
| `X-Dev-User-Id` is gated by `settings.KAIROS_DEV_AUTH_STUB_ENABLED`, checked INSIDE `StubUserIdAuthentication.authenticate()` at request time — not by which authenticator classes a settings module happens to register | A class-list-based gate (e.g. only registering the stub authenticator in test settings) would be indistinguishable, from the DoD's own wording, from a genuine environment-scoped security boundary — but a future refactor moving class registration around could silently re-enable it anywhere. Checking a dedicated flag at call time makes the boundary explicit and independently testable. Verified two ways: `test_x_dev_user_id_is_rejected_under_dev_settings` starts the actual app under `kairos.settings.dev` in a real subprocess and makes a real HTTP request against it (not a settings-flag unit test simulating dev), and the identical check was independently reproduced live via `curl` against a real `manage.py runserver` process in this same session | `kairos/identity/authentication.py`, `kairos/settings/{base,dev,test}.py`, `tests/identity/test_authentication.py` |
| ⚠️ **Revised after review, before merge**: the DoD literally says "every prior test updated to use real auth and still passing" — the first pass satisfied only "still passing" (kept the stub everywhere) and treated that as sufficient. It wasn't: two parallel, never-cross-tested paths ("old tests via stub," "new auth tests via real tokens") don't prove the write path actually works under real identity. Fixed by converting 8 representative existing tests — booking creation (the flagship one via the FULL mock-login → token-exchange round trip, not just a minted token), the create-conflict-409 path, edit, self-cancel, admin-override-cancel, IDEM-01/02, and the audit-attribution test AUD-03(a) — to `_bearer_headers()`, a real minted session token verified by the real `OIDCSessionAuthentication` class. The remaining ~85 tests still use the (gated, test-only) stub deliberately: the two paths now demonstrably meet at write-path/session-settings/audit level, and rewriting every remaining call site would still be mechanical churn without adding coverage the auth LAYER doesn't already get from `tests/identity/`. CONC-01–05 are NOT candidates for conversion at all — they exercise the exclusion constraint via raw psycopg SQL with zero Django/HTTP/auth layer involved by design (confirmed: no `APIClient`, no auth header, anywhere in `tests/concurrency/`), so there is no authentication step in them to convert | `tests/bookings/test_views.py`, `test_cancel_edit.py`, `test_idempotency.py`, `test_history.py` (`_bearer_headers` helpers) |
| `AuthorizationService` gained `can_administer_resource` (system_admin OR scoped resource_admin) as a strictly BROADER check than Phase 6/7's original `is_resource_admin(...) or is_operations(...)` inline checks — system_admin can now also list-by-resource, cancel-override, and (implicitly) view/edit anywhere | PRD FR44 defines `system_admin` as global ("manages catalogue and scope assignment"); the pre-Phase-9 inline checks never actually consulted that role at all, an omission from before the role concept was fully wired up. Consolidating into one service surfaced and fixed this gap as a side effect, not a deliberately scoped-in feature — flagged here so it isn't mistaken for an intentional design decision made independently of the refactor | `kairos/identity/authorization.py` (`AuthorizationService.can_administer_resource`) |
| PRD FR46's "restricted resources" needed a `user_group`/`user_group_membership` schema Spec v1.0 §3 never defined at all (confirmed: zero matches for "group" or "restrict" in that document) | RFC v1.0 §8.2 gestures at a `resource_group_id` in an aspirational grant table, but the ACTUAL implemented `resource_admin` grant (Phase 2) is keyed on `resource_id` directly, not a group. Rather than retrofit `resource_admin` to a group model Spec never specified either, Phase 9 adds the minimal schema PRD FR46/SEC-06 concretely need: a named `user_group`, a plain membership M2M, and a nullable `resource.restricted_group` FK (null = open, matching every resource before this phase). Group MANAGEMENT (create a group, add/remove members) has no endpoint yet — see NOT Yet Built | `kairos/identity/models.py` (`UserGroup`, `UserGroupMembership`), `kairos/resources/models.py` (`Resource.restricted_group`) |
| `KAIROS_SESSION_SIGNING_KEY` falls back through THREE tiers — explicit env var, then `SECRET_KEY`, then a hardcoded dev-only literal — with `prod.py` refusing to start if the literal is ever what's actually in play | Caught empirically, not by inspection: the original two-tier fallback (env var, else `SECRET_KEY`) produced an EMPTY string in dev/test, because `SECRET_KEY` itself defaults to `""` when `DJANGO_SECRET_KEY` isn't set locally — and PyJWT refuses to sign with an empty HMAC key, so the very first login attempt in a fresh test run raised `InvalidKeyError`. The third tier fixes dev/test without weakening prod, which already required `SECRET_KEY` non-empty and now requires this key not be the literal fallback too | `kairos/settings/base.py`, `kairos/settings/prod.py` |
| `local_to_instant(local_dt, zone, on_date)` takes `on_date` as a SEPARATE, authoritative argument rather than reading the date off `local_dt` | RFC v1.0 §9.1's exact bug is computing an occurrence's offset using the date the *request* (or series) was created on rather than the occurrence's own date. Making `on_date` a distinct parameter — always the one consulted for the offset, never `local_dt`'s own date component — makes that bug structurally unreachable through this function rather than merely avoided by caller discipline, the same "can't be bypassed" bar the exclusion constraint itself is held to. TZ-02 asserts this directly: `local_dt` deliberately carries Oct 20 (the creation date); only `on_date` (Nov 10) decides the offset | `kairos/core/timezones.py` (`local_to_instant`) |
| `validate_iana_zone` checks membership in `zoneinfo.available_timezones()` rather than a regex rejecting offset-shaped strings | A regex could reject `+01:00` but would accept any other garbage that merely isn't offset-shaped; membership in the real IANA catalog is the actual PRD FR8 requirement ("an offset cannot express when rules change") and costs nothing extra since `zoneinfo`/`tzdata` are already required dependencies. A Postgres CHECK constraint was considered and rejected: Postgres forbids subqueries (e.g. against `pg_timezone_names`) in CHECK constraints because they aren't immutable, so DB-level enforcement of full IANA membership isn't achievable the way the exclusion constraint is — this is application-level validation on the one write path that exists, not a deliberately weaker tier of the same guarantee | `kairos/core/timezones.py` (`validate_iana_zone`) |
| `validate_iana_zone` raises the existing `PolicyValidationError` (from `kairos/core/exceptions.py`) directly, not Django's `django.core.exceptions.ValidationError` | `PolicyValidationError` is already the framework-agnostic `{"field","issue"}` exception every write path raises, translated to 400 `validation_error` by `kairos_exception_handler`. Reusing it means Phase 19's future resource-write serializer needs zero adaptation — calling `validate_iana_zone` from `serializer.validate()` produces the correct 400 response on day one, the same pattern `PolicyValidationError`'s own docstring already describes | `kairos/core/timezones.py`, `kairos/core/exceptions.py` |
| `Resource.save()` is overridden to call `validate_iana_zone(self.timezone)` unconditionally, before `super().save()` | Phase 19 (resource-write endpoint) doesn't exist yet — the only live write path today is direct ORM construction (test fixtures, and Phase 19's future service layer). Validating in `save()` rather than only in a not-yet-written serializer means the check is already active and already tested, and Phase 19 inherits it for free instead of needing to remember to add it | `kairos/resources/models.py` (`Resource.save`) |
| `tzdata` is pinned with `==`, not a range, and a dedicated CI-form test (`tests/test_timezones.py`) asserts both the pin's exactness and that the installed version matches it | Test Plan TZ-03 Test A: "the deployed tzdata version is explicitly pinned and recorded... not 'whatever the base image shipped.'" A range (`>=`) would let CI silently resolve a newer release over time, reintroducing exactly the untracked-staleness failure mode TZ-03 exists to catch. The version is also logged at startup via `CoreConfig.ready()` (verified live via `manage.py check`, not just by inspection) so staleness is visible in production logs too, not only in CI | `backend/pyproject.toml`, `kairos/core/apps.py`, `tests/test_timezones.py` |
| TZ-04 is tested against `GET /resources/{id}/availability` with two different authenticated users, not against a `booking` detail endpoint | Spec v1.0 §5.7: availability is viewable by "any authenticated user," unlike booking detail (owner/admin/operations only, 404 otherwise per SEC-01) — TZ-04's actual claim ("no per-viewer localization exists") needs two viewers who can BOTH legitimately see the same data, which only the availability endpoint (or resource detail) provides without also entangling authorization logic into the assertion | `tests/test_timezones.py` |
| PRD FR7's second sentence ("store the IANA timezone identifier under which [a one-off booking] was created") is NOT implemented — flagged as a documented gap, not built | Symmetrical with Phase 9's `user_group` gap: Spec v1.0 §3's `booking` DDL has no column for it at all, and — unlike FR46/SEC-06 in Phase 9 — this phase's own Scope IN/DoD (as literally given) never calls for adding one, unlike its "Documents satisfied" line which names FR7 in full. Building unrequested schema/API surface beyond the given scope would be scope creep the same way silently dropping a named requirement would be a silent gap; flagging it here is the honest middle path. See Open Questions | Spec v1.0 §3 (no such column); PRD v1.0 FR7 |

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
                             # see README.md for the full auth + booking curl walkthrough
```

Auth (Phase 9): `POST /api/v1/auth/dev-mock-login` (dev/test only) mints a mock OIDC ID
token; `POST /api/v1/auth/token` exchanges it for the session token every other endpoint's
`Authorization: Bearer <token>` expects. See README.md for the exact sequence.

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

`tests/identity/` (Phase 9) — `test_authentication.py`: the real OIDC login flow end to end
through the local mock provider (`dev-mock-login` → `token` → an authenticated request with
the resulting session token), rejection of a malformed/forged/expired ID token and an
expired/unknown-subject session token, `test_real_oidc_principal_reaches_app_actor_id_at_key_
claim_insert` (the same spy-on-cursor style as Phase 7/8's session-settings regression test,
now proving a REAL authenticated principal's id — not a stub — reaches `app.actor_id` at the
key-claim INSERT, through the identical shared mechanism), and `test_x_dev_user_id_is_
rejected_under_dev_settings` (starts the actual app under `kairos.settings.dev` in a real
subprocess and makes a real HTTP request against it — not a settings-flag simulation).
`test_authorization.py`: PRD FR44's four roles and FR45's scoped-admin isolation, including
`test_scoped_admin_cannot_cancel_booking_on_resource_they_do_not_administer` exercised through
the real cancel endpoint, not just the service-level check. `tests/test_security.py`
(Phase 9) — SEC-01 (GET/PATCH/cancel/history against another user's booking: 404 on every
verb, and the response body's exact key set asserted, not just its status code) and SEC-06
(a restricted resource: 404 on direct access AND absent from list results for a non-member,
including the booking-creation and availability paths; a group member and the resource's own
admin can still see it).

`tests/test_timezones.py` (Phase 10) — TZ-02 as a direct unit test of `local_to_instant`
(the exact America/New_York Oct-20-creation/Nov-10-occurrence case resolves to
`2026-11-10T15:00:00Z`); nonexistent/ambiguous detection against the exact Europe/Paris
2027-03-28/2027-10-31 dates Test Plan TZ-05/TZ-06 use; `validate_iana_zone` accepting a real
zone and rejecting a fixed offset, both directly and through `Resource.save()` (the DoD's
"submitted → 400" case, proven at the model layer since no resource-write endpoint exists
yet — see Key Technical Decisions); the tzdata-pin CI form (Test Plan TZ-03 Test A) —
asserts the `pyproject.toml` pin is exact (`==`, not a range) AND that the installed
`tzdata` version matches it; and TZ-04 as a real HTTP test against
`GET /resources/{id}/availability`, asserting two different authenticated users receive
byte-identical UTC `busy_blocks`.

Also runnable: `cd backend && ruff check . && ruff format --check . && mypy kairos` (all pass
with zero findings as of Phase 10). CI (`.github/workflows/ci.yml`) runs all of this as three
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

From Phase 9:

- **PRD FR46 ("a resource may be restricted to a user group") has no corresponding schema
  anywhere in Spec v1.0 §3** — confirmed directly: zero occurrences of "group" or "restrict"
  in that document at all. RFC v1.0 §8.2 gestures at a `resource_group_id` inside an
  aspirational authorization grant table, but the ACTUAL `resource_admin` grant Phase 2
  implemented is keyed on `resource_id` directly, not any group concept — so even the RFC's
  own gesture doesn't match what got built. This is a genuine gap in the source document set,
  not an oversight in this phase's implementation. Phase 9 resolved it by adding the minimal
  schema PRD FR46 and Test Plan SEC-06 concretely need — `user_group`, `user_group_membership`
  (plain M2M), and a nullable `resource.restricted_group` FK (null = open, the default and the
  state of every resource created before this phase). **This is deliberately minimal**: there
  is no group-MANAGEMENT endpoint (create a group, add/remove a member) — those rows are
  ORM-created directly in tests, same as `resource_admin` was before Phase 19 gives it one.
  **Whichever phase builds admin-facing resource/group management (most likely Phase 19,
  "Resource Administration & Offboarding") should treat this schema as already decided** —
  extend it, don't redesign it, and don't let a differently-shaped group model creep in
  without updating this entry and the Key Technical Decisions row that explains the choice.

From Phase 10:

- **PRD FR7's second sentence has no corresponding column in Spec v1.0 §3's `booking` DDL.**
  FR7 reads: "A one-off booking is an instant range. Store as UTC. Additionally store the
  IANA timezone identifier under which it was created, for display and audit." The first two
  sentences are satisfied (`time_range TSTZRANGE`, since Phase 2). The third has no
  `booking` column for it at all, and — unlike the FR46/`user_group` gap above — Phase 10's
  own Scope IN and Definition of Done (as given) never call for adding one, even though its
  "Documents satisfied" line names FR7 in full. Nothing was built for it this phase: no
  `booking.created_timezone` column, no `timezone` field on `POST /api/v1/bookings`'s request
  body (Spec v1.0 §5.1's example body is `resource_id`/`start`/`end` only, already UTC — the
  client doesn't send a zone today). **No phase in the current 31-phase plan is explicitly
  scoped to pick this up.** The nearest natural point is Phase 12 (Recurring API — Preview &
  Confirm), which builds the analogous timezone-storage path for `recurring_series` and is
  the next time `BookingResponseSerializer`/booking-creation is touched at all — Phase 12
  should either add `booking.created_timezone` then (client sends the creating zone, or it's
  inferred from `resource.timezone`) or explicitly re-defer it in its own session's CLAUDE.md
  update, restating this entry rather than letting it silently drop. If Phase 12 passes
  without addressing it, it should be carried forward to a dedicated polish/cleanup phase
  instead of being assumed resolved. It must not be silently assumed to already exist, and
  must not be added incidentally as a side effect of unrelated work without updating this
  entry.

Genuine open questions from the source documents (offer window duration, nonexistent-time
policy default, series bounds, etc.) are tracked in PRD v1.0 §11 and RFC v1.0 §18; they get
resolved or explicitly deferred as the relevant phases are built.
