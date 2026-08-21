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
│   │   ├── celery.py       # Celery app (Phase 13) — kairos/__init__.py wires it per
│   │   │                   # Celery's own standard Django integration pattern
│   │   ├── settings/      # base.py (+ CELERY_* / CELERY_BEAT_SCHEDULE — Phase 13), dev.py,
│   │   │                   # test.py, prod.py — DRF + logging wired in (Phase 4)
│   │   ├── core/           # constants.py, exceptions.py, drf.py, logging.py, middleware.py,
│   │   │                   # tasks.py (check_tzdata_drift_task — Phase 13; the module
│   │   │                   # autodiscover_tasks() actually finds, see Key Technical
│   │   │                   # Decisions for why tzdata_check.py's own task didn't work),
│   │   │                   # tzdata_check.py (fetch_latest_tzdata_version — verified live
│   │   │                   # against PyPI, check_tzdata_drift — Phase 13, Test Plan
│   │   │                   # TZ-03 Test B),
│   │   │                   # db.py (write-path session settings + audit actor propagation,
│   │   │                   # ONE shared apply_write_path_session_settings — Phase 8),
│   │   │                   # idempotency.py (run_idempotent_write — Phase 5;
│   │   │                   # run_idempotent_recurring_confirm — Phase 12, a genuinely
│   │   │                   # different transaction shape, not a variant), models.py
│   │   │                   # (IdempotencyKey since Phase 5,
│   │   │                   # AuditLog since Phase 8), migrations/0002-0003 (audit_log table,
│   │   │                   # kairos_app role + grants, write_audit_log() trigger — Phase 8),
│   │   │                   # timezones.py (validate_iana_zone, local_to_instant,
│   │   │                   # is_nonexistent_local_time/is_ambiguous_local_time,
│   │   │                   # tzdata_version — Phase 10, consumed by Phase 11's recurrence
│   │   │                   # engine), apps.py (CoreConfig.ready() logs tzdata_version at
│   │   │                   # startup — Phase 10), migrations/0005 (kairos_app grants on
│   │   │                   # recurring_series — Phase 11), models.py (+ SystemCheckRun —
│   │   │                   # Phase 13, all six Spec check_name values, only two get a real
│   │   │                   # writer yet), migrations/0006-0007 (system_check_run table +
│   │   │                   # kairos_app grants — SELECT/INSERT only, append-only like
│   │   │                   # audit_log — Phase 13), migrations/0008-0009 (waitlist_entry/
│   │   │                   # waitlist_offer grants + audit triggers — Phase 14/16),
│   │   │                   # exceptions.py's RecordableConflictError (Phase 16) — the common
│   │   │                   # base SlotUnavailableError/AlreadyOnWaitlistError/
│   │   │                   # OfferExpiredError/OfferAlreadyResolvedError all share, letting
│   │   │                   # drf.py and idempotency.py each hold ONE generic branch instead
│   │   │                   # of one per exception (consolidated once a 4th instance of the
│   │   │                   # pattern appeared, not before — see Key Technical Decisions),
│   │   │                   # notifications.py (NotificationService + notify_offer_created/
│   │   │                   # notify_admin_cancellation/notify_rematerialization/
│   │   │                   # notify_rematerialization_conflict/notify_rollback_hold_released
│   │   │                   # — Phase 18, PRD FR52-55; delivery reuses Django's own console/
│   │   │                   # smtp/locmem EMAIL_BACKEND rather than a bespoke backend
│   │   │                   # abstraction), models.py (+ NotificationLog — Phase 18, the
│   │   │                   # PRD FR55 "recorded and retried" mechanism), migrations/0010-0011
│   │   │                   # (notification_log table + kairos_app grants — SELECT/INSERT/
│   │   │                   # UPDATE, no audit trigger, Phase 18), tasks.py's
│   │   │                   # send_notification_task/dispatch_notification (Phase 18, the
│   │   │                   # SAME dispatch-wrapper-swallows-broker-failures shape
│   │   │                   # kairos.waitlist.tasks.dispatch_cascade established in Phase 17),
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
│   │   ├── bookings/       # booking (+ series FK, populated since Phase 12), recurring_series
│   │   │                   # (Phase 11 schema, real write path since Phase 12),
│   │   │                   # recurrence.py (expand_occurrences — pure, DST-safe, still no
│   │   │                   # API surface of its own — Phase 11), recurring_series.py
│   │   │                   # (preview/confirm orchestration + preview_token issue/verify —
│   │   │                   # Phase 12), services.py (create/edit/cancel — Phase 7;
│   │   │                   # cancel_recurring_series — Phase 12), serializers.py,
│   │   │                   # views.py (BookingHistoryView — Phase 8; recurring preview/
│   │   │                   # confirm/cancel views — Phase 12), urls.py, tasks.py
│   │   │                   # (rolling_materialize_series/rematerialize_stale_series —
│   │   │                   # Phase 13, the first writes NOT initiated by an HTTP request),
│   │   │                   # management/commands/rematerialize_series.py (the "on deploy"
│   │   │                   # trigger RFC §9.4 asks for, alongside the scheduled Beat entry
│   │   │                   # — same functions, not a second implementation — Phase 13),
│   │   │                   # create_booking gained a `status` field (Phase 15) — status=HELD
│   │   │                   # is a hold (RFC §10.1), same INSERT/session-settings/SQLSTATE
│   │   │                   # machinery as a confirmed booking, expires_at computed from
│   │   │                   # OFFER_WINDOW_MINUTES, load-bearing comment at the call site.
│   │   │                   # cancel_booking's on_commit hook (Phase 7 log stub) now really
│   │   │                   # dispatches kairos.waitlist.tasks.dispatch_cascade (Phase 16, RFC
│   │   │                   # §5c step 4). create_booking also runs the cleanup-on-write DELETE
│   │   │                   # (Phase 17, RFC §10.4 mechanism 1) inside its own transaction,
│   │   │                   # before every INSERT, unconditionally, for every caller.
│   │   │                   # cancel_booking ALSO registers a second on_commit hook — PRD FR53,
│   │   │                   # Phase 18 — dispatching notify_admin_cancellation, but ONLY when
│   │   │                   # actor_type=ADMIN (a real resource-admin override; a self-cancel
│   │   │                   # never reaches it). tasks.py's rematerialize_stale_series now
│   │   │                   # dispatches notify_rematerialization on a successful recompute and
│   │   │                   # notify_rematerialization_conflict to the series owner AND every
│   │   │                   # resource admin on a conflict (Phase 18, PRD FR54/FR13b — closes
│   │   │                   # the "resource_administrators: pending Phase 18 delivery"
│   │   │                   # placeholder Phase 13 left)
│   │   ├── waitlist/       # waitlist_entry (Phase 14), waitlist_offer (Phase 16) — its own
│   │   │                   # app, not part of bookings/, since Spec's own ER diagram reaches
│   │   │                   # both directly from app_user/resource, never through booking (see
│   │   │                   # Key Technical Decisions). models.py (WaitlistEntry, db_default
│   │   │                   # joined_at — SEC-03(a); WaitlistOffer — Phase 16, hold_booking a
│   │   │                   # OneToOneField per Spec's UNIQUE hold_booking_id),
│   │   │                   # migrations/0002 (uniq_live_waitlist_per_user_slot, raw SQL —
│   │   │                   # 32-char name, same E033 precedent as bookings/0004), 0003
│   │   │                   # (waitlist_offer — Phase 16, the "no EXCLUDE constraint, and why"
│   │   │                   # comment reproduced verbatim per that phase's own Scope IN),
│   │   │                   # services.py (join_waitlist/cancel_waitlist_entry,
│   │   │                   # find_eligible_entries — the @> containment query, PRD FR21, first
│   │   │                   # real caller in Phase 16; create_offer_for_freed_range/
│   │   │                   # accept_offer/decline_offer — Phase 16; reap_expired_holds/
│   │   │                   # hold_reaper_heartbeat_is_stale — Phase 17, RFC §10.4 mechanism 2),
│   │   │                   # tasks.py (create_offer_for_freed_range_task — Phase 16, the SAME
│   │   │                   # tasks.py-must-be-named-that-for-autodiscovery lesson Phase 13
│   │   │                   # already established; deferred-import trick to avoid a real
│   │   │                   # circular import with kairos.bookings.services — see Key Technical
│   │   │                   # Decisions; dispatch_cascade — Phase 17, wraps .delay() so a
│   │   │                   # broker outage degrades rather than raises out of an on_commit
│   │   │                   # callback, WL-06; reap_expired_holds_task — Phase 17, on
│   │   │                   # CELERY_BEAT_SCHEDULE at HOLD_REAPER_INTERVAL_SECONDS),
│   │   │                   # services.py's create_offer_for_freed_range now dispatches
│   │   │                   # notify_offer_created right after the offer row commits (Phase 18,
│   │   │                   # PRD FR52 — expiry stated explicitly), called directly rather than
│   │   │                   # via on_commit since this function is never itself inside an open
│   │   │                   # transaction,
│   │   │                   # serializers.py (slot_already_available check before
│   │   │                   # the idempotency key is claimed; WaitlistOfferResponseSerializer —
│   │   │                   # Phase 16), views.py (join/list/cancel; WaitlistOfferConfirmView/
│   │   │                   # DeclineView — Phase 16), urls.py
│   │   ├── urls.py, wsgi.py
│   ├── tests/
│   │   ├── test_booking_exclusion_smoke.py
│   │   ├── test_schema_assertion.py   # RECON-05 CI form — fails if the predicate is narrowed
│   │   ├── test_audit_trail.py        # AUD-01, AUD-02, grant/trigger-existence checks (Phase 8)
│   │   ├── test_security.py           # SEC-01, SEC-06 (Phase 9)
│   │   ├── test_timezones.py          # TZ-02, TZ-04, TZ-03 Test A, IANA validation,
│   │   │                              # nonexistent/ambiguous detection (Phase 10)
│   │   ├── test_tzdata_check.py       # TZ-03 Test B — drift check alerts, never fails a
│   │   │                              # build (Phase 13)
│   │   ├── conftest.py                # app_user / active_resource fixtures, shared
│   │   ├── identity/                  # test_authentication.py (real OIDC flow, actor_id spy,
│   │   │                              # dev-settings-subprocess X-Dev-User-Id rejection),
│   │   │                              # test_authorization.py (four roles, scoped admin) — Phase 9
│   │   ├── bookings/                  # test_services.py, test_views.py, test_idempotency.py,
│   │   │                              # test_read_endpoints.py (Phase 6), test_cancel_edit.py
│   │   │                              # (Phase 7), test_history.py (AUD-03/04/05 — Phase 8),
│   │   │                              # test_recurrence.py (TZ-01/05/06/09/10 — Phase 11),
│   │   │                              # test_recurring_series.py (REC-01/07, IDEM-05,
│   │   │                              # per-occurrence audit proof — Phase 12),
│   │   │                              # test_rematerialization.py (TZ-07/08, rolling
│   │   │                              # materialization, actor_type='system' spy proofs —
│   │   │                              # Phase 13)
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
│   ├── Dockerfile          # Celery worker/beat image ONLY (Phase 13) — the API service
│   │                       # still runs via manage.py runserver on the host, unchanged
│   └── pyproject.toml      # ruff, mypy strict, pytest config; pyjwt[crypto] added Phase 9;
│                           # tzdata pinned exactly (==) added Phase 10; celery[redis] +
│                           # celery-types added Phase 13
├── frontend/              # React + TypeScript — empty until Phase 23
├── infra/                 # docker-compose.yml (+ redis/worker/beat services — Phase 13),
│                           # init-test-db.sql
└── scripts/
    └── spike/             # throwaway S1 spike scripts — will NOT be extended after Phase 1
```

Endpoints live (Phase 6 added the read path, Phase 7 the remaining single-booking mutations,
Phase 8 the history endpoint, Phase 9 real auth, Phase 12 recurring series, to Phase 4/5's
write path): `POST`/`GET /api/v1/bookings`, `GET`/`PATCH /api/v1/bookings/{id}`,
`POST /api/v1/bookings/{id}/cancel`, `GET /api/v1/bookings/{id}/history`,
`POST /api/v1/bookings/recurring/preview` (commits nothing — no Idempotency-Key),
`POST /api/v1/bookings/recurring` (207 Multi-Status, `Idempotency-Key` required),
`POST /api/v1/recurring-series/{id}/cancel`, `GET /api/v1/resources`,
`GET /api/v1/resources/{id}`, `GET /api/v1/resources/{id}/availability`,
`POST /api/v1/auth/token`, `POST /api/v1/auth/dev-mock-login` (dev/test only),
`POST /api/v1/waitlist-entries`, `GET /api/v1/waitlist-entries`,
`POST /api/v1/waitlist-entries/{id}/cancel` (Phase 14),
`POST /api/v1/waitlist-offers/{id}/confirm`, `POST /api/v1/waitlist-offers/{id}/decline`
(Phase 16). Every mutation (create, edit, cancel, recurring confirm, recurring-series cancel,
waitlist join, waitlist cancel, offer confirm, offer decline) is idempotent (Phase 5/7/12/14/16
— `Idempotency-Key` is required; missing it is 400). Edit is owner-only, no
admin override; cancel is owner-or-scoped-admin, with a reason required for the admin-override
case (400 otherwise). Cancelling an already-cancelled booking is a 200 no-op, independent of
idempotency key. Recurring series confirm attempts each occurrence in its OWN independent
transaction (RFC v1.0 §5d) — reusing `create_booking` (Phase 4) unchanged, which is what gives
every occurrence a fresh, correctly-timed session-settings application for free; a recurring
series's own cancel is owner(`created_by`)-or-scoped-admin, same shape as single-booking
cancel, and only touches still-confirmed future occurrences.

Waitlist join (Phase 14, Spec v1.0 §5.11): `POST /waitlist-entries` inserts a `waitlist_entry`
row directly (no availability check precedes it, same "the constraint/index IS the check"
philosophy as booking creation) — 409 `already_on_waitlist` on
`uniq_live_waitlist_per_user_slot` (covers both `waiting` and `offered`, RFC v1.0 §8.2), 422
`slot_already_available` (advisory check-then-act, evaluated in the serializer BEFORE the
idempotency key is claimed, same "don't consume a key slot on a request that can't succeed"
precedent as booking policy validation) when the range has no conflicting confirmed/held
booking at all. `joined_at` is a genuine DB-level `db_default`, never accepted from the
request body (SEC-03(a)) — there is no field for a client value to bind to. `GET
/waitlist-entries` is always self-scoped (no `user_id` param, Spec v1.0 §5.12) and reports
`queue_position` (PRD FR27, computed against `idx_waitlist_entry_order`'s own FCFS ordering)
and an `active_offer` field that is always `null` until Phase 16 gives it a real writer.
`POST /waitlist-entries/{id}/cancel` is owner-only self-withdrawal (Spec never describes an
admin override here, unlike booking cancel) — not literally in Spec v1.0 §5.11/§5.12, but
required by Phase 14's own Definition of Done; same idempotent-double-cancel-is-a-200-no-op
shape as booking cancel. `kairos.waitlist.services.find_eligible_entries` is the containment
(`@>`, PRD FR21) query the offer cascade (Phase 16) now consumes — proven directly first
(WL-04, Phase 14), the "mechanism before its real caller" pattern already used for
`actor_type='system'` (Phase 8→13).

Offers (Phase 16, RFC v1.0 §10.2/§10.3, Spec v1.0 §4.2/§4.3/§5.13): cancelling a booking now
really does dispatch `create_offer_for_freed_range` via `transaction.on_commit()` →
`create_offer_for_freed_range_task.delay()` (RFC v1.0 §5c step 4 — Phase 7's log-only stub is
gone), which walks `find_eligible_entries`' FCFS-ordered candidates, attempts a hold via
`create_booking(status=HELD, actor_type=SYSTEM)` for EACH in turn, and creates the
`waitlist_offer` row only once a hold actually succeeds (PRD FR23: hold before offer,
structurally — no `WaitlistOffer` is ever constructed for a hold that didn't commit) —
`SlotUnavailableError` (23P01) on a candidate's hold attempt just advances to the next one
(Spec v1.0 §4.2's own "re-query and try the next candidate," the identical retry-on-conflict
pattern used everywhere else in this design). `POST /waitlist-offers/{id}/confirm` runs
`accept_offer` — the literal RFC v1.0 §10.3/Spec v1.0 §4.3 conditional `UPDATE` (status, owner,
AND `expires_at > now()` all guarding the SAME statement) — no `slot_unavailable` is
structurally possible here (there's no INSERT on this path); 0 rows → 409 `offer_expired`,
recorded for idempotent replay like any other `RecordableConflictError`. Response is the
BOOKING (not the offer), with `waitlist_offer_id` added at the response layer only for this
one endpoint, since `booking` has no such column (Spec v1.0 §3's DDL doesn't define one).
`POST /waitlist-offers/{id}/decline` releases the hold immediately (`booking.status`→
`cancelled`, `expires_at`→`NULL` — the DB's `hold_has_expiry` CHECK requires this, a real bug
caught building WL-02, see Key Technical Decisions) and dispatches the SAME cascade worker for
the now-freed range — unlike every OTHER cancel-shaped endpoint in this codebase, an
already-resolved offer is NOT a 200 no-op; Spec v1.0 §5.13 names `offer_already_resolved` as
its own distinct 409. The declining entry's own status becomes `expired` (not back to
`waiting`) — otherwise it would immediately out-compete the very cascade its own decline
triggers, since it retains the earliest `joined_at` in the FCFS ordering; this is also what
gives `WaitlistEntryStatus.EXPIRED` (present in the schema since Phase 14, unused until now)
its actual meaning.

Hold reclamation (Phase 17, RFC v1.0 §10.4) — both mechanisms, because a constraint predicate
cannot express expiry (`now()` isn't IMMUTABLE) and neither alone suffices. Mechanism 1,
cleanup-on-write: `create_booking` now DELETEs any expired hold overlapping the range it's
about to write, inside the SAME transaction, before the INSERT — unconditionally, for every
caller (an ordinary booking, a recurring occurrence, rolling materialization, the cascade
worker's own hold creation). This is the self-healing property RECLAIM-01 proves: a stalled
reaper, or Redis being down entirely, never makes a resource permanently unbookable, because
the very next writer clears the stale hold themselves. Mechanism 2, the reaper:
`reap_expired_holds` (`kairos.waitlist.services`), on `CELERY_BEAT_SCHEDULE` at
`HOLD_REAPER_INTERVAL_SECONDS` (default 30s) — walks every expired hold, reclaims each via the
IDENTICAL conditional-UPDATE-to-`cancelled` shape `accept_offer` itself races against
(`WHERE status='held' AND expires_at<=now()`), and on a genuine win (not lost to a concurrent
acceptance) marks the offer `expired`, the entry `expired`, and cascades to the next eligible
entry via `create_offer_for_freed_range` — the SAME worker cancellation and decline already
use. One independent transaction per hold, so one contested hold losing its race to acceptance
never rolls back the reclamation of every other expired hold in the same sweep. Every run
writes a `system_check_run` row (`check_name='hold_reaper'`) regardless of whether anything
was found — the heartbeat itself, not an alert (full alerting is Phase 21); `hold_reaper_
heartbeat_is_stale` reads that heartbeat back and is the actual mechanism behind WL-05 Part B,
scoped honestly to what Phase 17 owns (the DATA a future alert would consume), not the
alert-firing/`GET /admin/checks/latest` surface itself (Phase 21). `cancel_booking`'s and
`decline_offer`'s cascade dispatch both now go through ONE new function, `dispatch_cascade`,
which wraps `.delay()` in a broad `try/except` and logs `cascade_dispatch_failed_broker_
unavailable` on failure rather than letting a broker outage propagate out of a
`transaction.on_commit()` callback and turn an already-committed, successful cancel/decline
into an apparent request failure — verified LIVE against a real Redis outage, not simulated
(see Key Technical Decisions and the Phase 17 Completed Phases row for the full transcript).

Notifications (Phase 18, PRD v1.0 FR52-55; RFC v1.0 §15a) — every notification point RFC
§15a/PRD FR52-54 name now actually sends, asynchronously, never from a request-path
transaction: `kairos/core/notifications.py`'s `notify_offer_created`/`notify_admin_
cancellation`/`notify_rematerialization`/`notify_rematerialization_conflict` each build a
subject/body and hand off to `kairos/core/tasks.py`'s `dispatch_notification`, which enqueues
`send_notification_task.delay(...)` and returns immediately — the actual `send_mail()` call
happens inside that Celery task, in a worker process. Delivery reuses Django's own
`EMAIL_BACKEND` abstraction rather than a bespoke one: console in dev, `locmem` (Django's own
capturing backend, `django.core.mail.outbox`) in test, real SMTP in prod (`SMTP_HOST`/`PORT`/
`USER`/`PASSWORD` — prod refuses to start without `SMTP_HOST` set, the same "refuse a
dev-shaped default" discipline `SECRET_KEY`/`KAIROS_SESSION_SIGNING_KEY` already have).
`send_notification_task` is `autoretry_for=(Exception,)` with `retry_backoff=True` (PRD
FR55's "recorded and retried") — every attempt, success or failure, updates ONE
`NotificationLog` row (`kairos/core/models.py`, new schema — Spec v1.0 §3 has no notification
concept at all, the same kind of gap Phase 9's `user_group` table filled) via `attempts`
accumulating and `status` reflecting the latest outcome. `dispatch_notification` wraps
`.delay()` in the identical try/except shape `kairos.waitlist.tasks.dispatch_cascade` (Phase
17) already established, so a broker outage degrades (logged) rather than propagating out of
`cancel_booking`'s `transaction.on_commit()` callback and turning an already-committed
cancellation into an apparent failure. Offer-created states the expiry EXPLICITLY, in the
subject line itself (PRD FR52); admin-cancellation only fires for a genuine resource-admin
override (`actor_type=ADMIN`), never a self-cancel, and includes the recorded reason (PRD
FR53); the tzdata re-materialization pair closes a promise Phase 13's own conflict-recording
code carried since it was written ("resource_administrators: pending Phase 18 delivery") —
`notify_rematerialization` fires per successfully-recomputed occurrence, `notify_
rematerialization_conflict` fires to BOTH the series owner and every resource administrator
scoped to that specific resource on a conflict (PRD FR13b). A fifth type,
`notify_rollback_hold_released` (Rollout v1.0 §4.5), exists and is fully tested standalone —
distinct, non-generic wording from an ordinary offer-expiry notification, explicitly stating
queue position was preserved — but has NO real production caller: §4.5's hold release is a
manual operational runbook (SQL an operator runs during an incident), not application code any
phase has built, and inventing a fake trigger path just to have one would have been scope
beyond this phase's actual job (see NOT Yet Built and Open Questions).

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

Every state transition on `booking`/`resource`/`resource_admin`/`waitlist_entry`/
`waitlist_offer` is audited (Phase 8, `waitlist_entry` added Phase 14, `waitlist_offer` added
Phase 16): the `write_audit_log()` trigger fires on every INSERT/UPDATE/DELETE against those
tables, unconditionally — including a raw SQL write that never touches the service layer at
all.
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
| 11 | Recurrence Materialization & DST ⚠️ Subtle | `kairos/bookings/recurrence.py`'s `expand_occurrences` — a PURE function, no DB writes, no conflict checking — computes each occurrence's date via plain date arithmetic (`series_start_date + 7*i days`, never touching a UTC instant), THEN localizes that specific date's wall-clock time using `kairos/core/timezones.py`'s Phase 10 utilities, with a load-bearing comment citing RFC v1.0 §9.2 on the function itself. A nonexistent local time (PRD FR11) is shifted forward by a DYNAMICALLY COMPUTED transition gap (`fold=0` instant minus `fold=1` instant at that wall-clock value — not a hardcoded 1 hour) applied to BOTH start and end, preserving the occurrence's local duration; an ambiguous local time (PRD FR12) needs no shift at all, since `local_to_instant`'s default `fold=0` already IS the first/pre-transition instance — only disclosure is added. `RecurringSeries` (`kairos/bookings/models.py`) reproduces Spec v1.0 §3's DDL field-for-field, including the two columns v0.1 omitted (`series_start_date`, `tzdata_version`) plus `materialized_through`/`occurrence_count CHECK BETWEEN 1 AND 100`; `Resource.save()`'s Phase 10 IANA-validation pattern is reused verbatim in `RecurringSeries.save()`. `Booking.series` (nullable FK) exists now — `BookingResponseSerializer.get_series_id`'s Phase-9-era stub (hardcoded `None`, explicitly commented "doesn't exist until Phase 11") is wired up to the real column. `occurrence_count` is validated in TWO layers: `expand_occurrences` raises `PolicyValidationError` before any DB touch (satisfies "101 → validation error" without needing a `RecurringSeries` row at all, which matters because Phase 12's preview endpoint commits nothing), backed by the DB `CHECK` constraint from Spec's own DDL as a bulk/raw-SQL backstop. `idx_series_materialized_through` (32 characters) exceeds Django's 30-character portability limit for `models.Index` (E034) — added via a dedicated raw-SQL migration instead of shortening the name, matching Spec v1.0 §3 verbatim (Postgres itself allows up to 63). A new `core` migration grants `kairos_app` access to `recurring_series`, following the exact Phase 8/9 "GRANT statements aren't retroactive" pattern — verified live by connecting AS `kairos_app` and both SELECTing and INSERTing (rolled back) against the table. TZ-01 (America/New_York, the Nov-1 transition date itself as an occurrence, resolving EST not EDT), TZ-05 (Paris nonexistent, shifted 02:30→03:30), TZ-06 (Paris ambiguous, first/pre-transition instance chosen), TZ-09 (Sydney, both October/April transitions — opposite hemisphere, catches a sign error every northern test would miss), and TZ-10 (Kolkata, zero DST, identical offset throughout) all pass with the exact dates from Test Plan §5. No API surface yet, by design (Implementation Plan's own "why this is its own phase" — isolating DST correctness from conflict-handling concerns); materializing a confirmed series into real `booking` rows and surfacing per-occurrence conflicts (PRD FR10) are Phase 12's job. Full suite: 146 tests (140 + 6 concurrency), 12 new, no regressions | Pending (on branch `phase-11-recurrence-dst`) |
| 12 | Recurring API — Preview & Confirm 🏁 Milestone 2 | `POST /bookings/recurring/preview` (Spec §5.8) commits NOTHING — not even a preview-tracking row — because the entire series definition plus the computed conflict-date and adjustment-date sets travels inside a signed, short-lived `preview_token` (HS256, `KAIROS_SESSION_SIGNING_KEY` — the same signing key the Phase 9 session token already uses, not a third key). `POST /bookings/recurring` (Spec §5.9) decodes that token and re-runs the IDENTICAL `expand_occurrences` call with the IDENTICAL inputs — REC-07's "preview and confirm use the same expansion code path" is true by construction, not by two implementations happening to agree, and is proven live (not just in tests): a real `curl` preview→confirm round trip produced byte-identical instants both times. PRD FR33's explicit-acknowledgment gate (`unacknowledged_conflicts`, 409) is checked BEFORE the idempotency key is ever claimed and before any occurrence is attempted (REC-02: zero bookings created), mirroring Phase 4's "policy validation before key-claim" precedent exactly. RFC §5d's per-occurrence transaction requirement is satisfied by REUSING `create_booking` (Phase 4) unchanged inside confirm's loop — not a new write function — so each occurrence gets its own transaction and its own fresh `apply_write_path_session_settings` call for free, the exact property Phase 12's own instructions demanded be confirmed explicitly; proven by REC-05 (a real pre-existing booking on occurrence 7 doesn't roll back occurrence 6 — the only possible outcome if they shared a transaction is BOTH being rolled back, since any statement failure aborts the whole transaction in Postgres absent a savepoint, and none exists here), plus a dedicated audit test confirming one row per occurrence, correctly attributed. ⚠️ **Correction, found during Phase 13**: this row originally also cited DISTINCT `occurred_at` timestamps (4 audit rows at 21:47:52.106333/.166871/.207928/.252695) as proof of independent transactions. Verified directly against a real connection: Django's `Now()` compiles to Postgres's `statement_timestamp()`, which advances on every `cursor.execute()` regardless of transaction boundaries — N separate INSERTs inside ONE shared transaction would ALSO produce N distinct timestamps, so this was never actually evidence of what it was cited for. The test's docstring and this entry are corrected; REC-05 remains the real, airtight proof and was never in question. Idempotency (IDEM-05) needed a GENUINELY NEW mechanism, `run_idempotent_recurring_confirm` — not a variant of `run_idempotent_write` — because the key claim can no longer live in the same transaction as "the write" when there are N independent writes; the key is claimed and committed in its own transaction FIRST, occurrences are attempted, and the outcome is recorded in a THIRD transaction after, which means (documented, not silently assumed away) a crash mid-series leaves the key permanently `in_progress` with no automatic recovery beyond the existing 24h cleanup command. Occurrences the preview already knew conflicted are reported as `acknowledged: true` conflicts with NO retry attempt at confirm time (the user's acknowledgment IS the agreement they won't be created); only occurrences the preview reported clean are actually attempted, so a NEW conflict arising between preview and confirm (REC-03) is structurally distinguishable (`acknowledged: false`) from an already-known one. `POST /recurring-series/{id}/cancel` (Spec §5.10) reuses the EXISTING single-transaction `run_idempotent_write` (not the new confirm mechanism) because cancellation can never lose to the exclusion constraint — moving rows OUT of `status IN ('confirmed','held')` never conflicts — so there is no "one contested item" isolation problem to solve, unlike creation; a single bulk `UPDATE ... WHERE id IN (...)` still produces one audit row per cancelled booking, since Postgres's row-level trigger fires per row regardless of statement count. REC-01 through REC-07 and IDEM-05 all pass, plus REC-06's full bound matrix (0/100/101, within/beyond 365-day horizon) — the 100-occurrence case required starting the test series far in the past, since 100 WEEKLY occurrences always span 693 days end to end, structurally exceeding the 365-day horizon from "now" forward for ANY future start date; this surfaced, not invented, a real tension between REC-06's two independent bounds. Post-review revision: initially left series-cancel-by-admin without a required `reason`, matching Spec v1.0 §5.10's example body (which shows none) — re-examined on the question "is that genuinely out of scope, or a gap," and found it's actually DIRECTLY governed by PRD FR47 ("administrative override of another user's booking requires a recorded reason," unconditional, no series/single-booking carve-out) — closed by mirroring `BookingCancelSerializer`'s exact pattern (`RecurringSeriesCancelSerializer`). Bookable-hours/max-duration/past-dating validation was examined under the SAME question and found to be genuinely different in character, not merely unlisted in Spec's failure table: doing it correctly requires converting each occurrence into the RESOURCE's own timezone (which can differ from the series' `timezone` field) and checking per-occurrence, plus resolving an unanswered "reject the batch or report per-occurrence" design question — deferred explicitly to Phase 13, not invented under time pressure; see Key Technical Decisions/Open Questions. Full suite: 165 tests (159 + 6 concurrency), 19 new, no regressions; entire flow (preview, confirm, cancel, admin-override-with-reason) verified live over real HTTP against `manage.py runserver` connected as `kairos_app`, with ground truth read back via `psql`, not just through the test client | Pending (on branch `phase-12-recurring-preview-confirm`) |
| 13 | Rolling Materialization & tzdata Re-materialization | First background-worker infrastructure: `kairos/celery.py` (Celery app, `kairos/__init__.py` wires it per Celery's own standard Django pattern), `Dockerfile` (new — the app itself still runs via `manage.py runserver` on the host per README; this image is ONLY for the worker/beat services), `infra/docker-compose.yml` gains `redis`/`worker`/`beat`. `rolling_materialize_series` and `rematerialize_stale_series` (`kairos/bookings/tasks.py`) are the first writes in this codebase NOT initiated by an HTTP request — both REUSE `create_booking`/`edit_booking` (Phase 4/7/12) exactly as `confirm_recurring_series` does, extended with a new `actor_type` field (default `USER`, unchanged for every existing caller) so a background job can pass `AuditActorType.SYSTEM` with `actor_id=""` (the same `NULLIF(...,'')`-becomes-NULL convention already established for an absent `reason`) — proven with the EXACT spy-on-cursor style Phase 7/8/9 already established for session-variable checks (not inferred indirectly), reading `current_setting('app.actor_type'/'app.actor_id', true)` immediately before the real write, plus confirming the persisted `audit_log` row has `actor_type='system'`/`actor_id=NULL`. `system_check_run` (Spec v1.0 §3, all six `check_name` values reproduced in the CHECK constraint verbatim even though only `series_materialization`/`tzdata_rematerialization` get a real writer this phase) records every run's findings — `kairos_app` granted `SELECT`/`INSERT` only, matching `audit_log`'s append-only philosophy (a check run is a historical record, never revised after the fact). Occurrence-to-booking matching for re-materialization (TZ-07/08) is by NEAREST recomputed instant (within a 2-day tolerance — occurrences are 7 days apart, DST shifts are under 24h, so this is unambiguous) rather than reverse-deriving a local date from the stored (possibly wrong) instant, which would be fragile exactly where correctness matters most. A series with an unresolved re-materialization conflict keeps its OLD `tzdata_version` deliberately, so the NEXT run retries precisely the occurrences that didn't yet succeed. TZ-03 Test B (`kairos/core/tzdata_check.py`) compares the installed `tzdata` version against PyPI's live release feed — verified LIVE against the real `https://pypi.org/pypi/tzdata/json` endpoint (not just mocked), returning the correct current version; alerts via logging only, since Spec's `check_name` enum has no slot for "external staleness" as a concept distinct from `tzdata_rematerialization`'s "internal consistency" meaning. ⚠️ **Real bug caught by actually running `docker compose up`, not just importing the module**: `check_tzdata_drift_task` was originally defined directly inside `tzdata_check.py`, which Celery's `autodiscover_tasks()` never finds — it only scans a module literally named `tasks.py` per app. The worker started successfully and reported no error; the task was simply, silently absent from its registered task list — exactly the "no errors, just absence" failure mode RFC v1.0 §14 warns about, caught only by reading the worker's own startup banner. Fixed by moving the thin `@shared_task` wrapper into a new `kairos/core/tasks.py`, keeping the real logic in `tzdata_check.py`. Full live verification, not just `docker compose up` succeeding: dispatched both `check_tzdata_drift_task` (real PyPI call from inside the container) and `rolling_materialize_series_task` (real `kairos_app` Postgres connection from inside the container) via `.delay()` against the actually-running worker and confirmed both completed successfully in its logs. ⚠️ **Correction to Phase 12's own claims, found while designing this phase's spy test**: verified directly (`statement_timestamp()` behavior against a real connection) that Phase 12's audit test overclaimed distinct `occurred_at` values as proof of independent transactions — see that Completed Phases row and its Key Technical Decisions row, both corrected, plus the test's own docstring. No real caller produces a partially-materialized series yet — Phase 12's confirm still rejects a horizon-exceeding series outright (REC-06 still passes, untouched) — so rolling materialization is proven directly against series constructed at whatever `materialized_through` a future confirm revision would leave them at, the established "mechanism before its real caller" pattern. Full suite: 177 tests (171 + 6 concurrency), 12 new, no regressions | Pending (on branch `phase-13-rematerialization`) |
| 14 | Waitlist Entries & Containment Eligibility | New `kairos.waitlist` app (own app, not inside `bookings/` — Spec's own ER diagram reaches `waitlist_entry` directly from `app_user`/`resource`, never through `booking`; see Key Technical Decisions). `WaitlistEntry` reproduces Spec v1.0 §3's DDL: `joined_at` is a genuine `db_default=Now()` column, never `auto_now_add` (same Phase 8 `AuditLog.occurred_at` fix, applied here as the actual mechanism behind SEC-03(a) — there is no serializer field for a client value to bind to at all, not merely one that gets ignored). `uniq_live_waitlist_per_user_slot` (32 characters, past Django's 30-char portability limit — the identical `idx_series_materialized_through`/E033 situation from Phase 11) is added via its own raw-SQL migration; `idx_waitlist_entry_lookup` (GiST, `django.contrib.postgres.indexes.GistIndex`) and `idx_waitlist_entry_order` both fit under the limit and are ordinary `Meta.indexes`. `POST /waitlist-entries` (Spec v1.0 §5.11) inserts directly — no availability pre-check, the same "the constraint/index IS the check" philosophy Phase 4 established for booking creation — with 409 `already_on_waitlist` (new domain exception, SQLSTATE 23505 on the partial unique index) and 422 `slot_already_available` (new domain exception; advisory check-then-act, evaluated in `WaitlistJoinSerializer.validate()` BEFORE the idempotency key is claimed, mirroring `BookingCreateSerializer`'s "don't consume a key slot on a request that can't succeed" precedent). `GET /waitlist-entries` (Spec v1.0 §5.12) is always self-scoped (no `user_id` param — "no permission check to get wrong," Spec's own words) and reports `queue_position` (PRD FR27, batched one query per distinct resource on the page — RFC v1.0 §7.2's N+1 guard, same principle as Phase 6's availability view) and an `active_offer` field that's always `null` until Phase 16. `POST /waitlist-entries/{id}/cancel` — owner-only self-withdrawal, NOT literally in Spec v1.0 §5.11/§5.12 (which document only join and list) but required by this phase's own Definition of Done ("join, list, and cancel... all work"), built on `BookingCancelView`'s exact owner-only/idempotent-double-cancel-is-200 shape minus the admin-override branch Spec never describes for a waitlist entry. `kairos.waitlist.services.find_eligible_entries` is the load-bearing containment query (`@>`, not `&&` — PRD FR21, the phase's designated load-bearing comment site #4 per Implementation Plan §1.3) — no live caller yet (offer cascade is Phase 16), proven directly against ORM-created rows, the "mechanism before its real caller" pattern already used for `actor_type='system'` (Phase 8→13). `core/migrations/0008` grants `kairos_app` full DML on `waitlist_entry` (it transitions status in place, unlike append-only `audit_log`/`system_check_run`) and attaches `audit_waitlist_entry` — 0003's own comment had already promised this trigger "when those tables exist." `kairos.core.idempotency`'s `_record_conflict_outcome` was generalized (code/message/http_status now caller-supplied instead of hardcoded to `slot_unavailable`) so `already_on_waitlist` gets the identical Spec v1.0 §7-point-7 "conflict outcomes are recorded too" treatment `slot_unavailable` already had — anticipated by that module's own docstring, which already named Phase 14 as a future reuser. WL-04 passes directly against `find_eligible_entries` (a freed 10:00–10:30 does NOT make a 10:00–11:00 waitlister eligible; a freed 10:00–11:00 does); SEC-03(a)/(b)/(c) all pass. Full suite: 201 tests (195 + 6 concurrency), 24 new, no regressions — including a pre-existing AUD-02 test that hardcoded the three Phase-8 audited tables, updated (not silently left stale) to include `waitlist_entry` as a fourth | Pending (on branch `phase-14-waitlist-entries`) |
| 15 | Holds: The Shared Exclusion Domain ⚠️ CRITICAL | Makes a hold a REAL reservation by putting it in the SAME `booking` table, inside the SAME exclusion domain a confirmed booking occupies (RFC v1.0 §10.1) — the whole point of the v0.1→v1.0 redesign this phase implements: a separate `waitlist_offer`-table constraint (v0.1's design) cannot exclude against `booking`, so an ordinary user could take a slot out from under an outstanding offer. `BookingCreateRequest`/`create_booking` (`kairos/bookings/services.py`) gained a `status` field (default `CONFIRMED`, unchanged for every prior caller) rather than a bespoke `create_hold()` — a hold's INSERT needs the IDENTICAL session-settings/SQLSTATE-translation/`refresh_from_db` machinery a confirmed booking's does, the same "one write path, one correctness proof" reasoning Phase 13 already used for `actor_type`. `expires_at` is computed INSIDE `create_booking` from the new `OFFER_WINDOW_MINUTES` constant (`kairos/core/constants.py`, RFC v1.0 §10.5 / PRD open question 1, default 15) whenever `status=HELD`, keeping the `hold_has_expiry` DB CHECK invariant enforced in one place rather than trusting every future caller to set it consistently. The RFC v1.0 §10.1 load-bearing comment (Implementation Plan §1.3 item 3) lives at the `Booking.objects.create(...)` call site itself, in ADDITION to Phase 2's existing comment at the constraint's own definition — two different developers, editing two different files, each need the warning where THEY are looking. HOLD-01 ★ (the Test Plan's own "most important test in the document") passes as a real, end-to-end proof: a hold created directly (no offer worker exists until Phase 16 — the identical "mechanism before its real caller" pattern already used for `actor_type='system'` and containment eligibility), an unrelated user's REAL `POST /bookings` for the exact same range returns 409 `slot_unavailable` over real HTTP, and W's acceptance — the literal RFC v1.0 §10.3/Spec v1.0 §4.3 conditional `UPDATE ... WHERE status='held' AND user_id=$2 AND expires_at > now()`, executed directly since Phase 16 hasn't built its endpoint yet, per this phase's own explicit instruction — flips the SAME row (same `id`, `status` transitioned, `expires_at` now NULL, no second row). Two companion tests prove the predicate's other two guard clauses matter: an expired hold's acceptance affects 0 rows, and the wrong `user_id` affects 0 rows too (Spec's `user_id` clause — RFC v1.0 §10.3's own SQL snippet calls this column `held_for_principal`, which doesn't exist in Spec v1.0 §3's actual DDL; `booking.user_id` already serves this role, established at the model layer since Phase 2/3, this phase is just the first to literally exercise it here). HOLD-02 (50 barrier-released raw-SQL `booking` INSERTs against an actively held range, 50 runs) asserts ZERO successes UNCONDITIONALLY every run — a safety invariant, not CONC-01's zero-is-a-liveness-characteristic-so-retry pattern — via the same `tests/concurrency/harness.py` used by CONC-01–05, mirroring CLAUDE.md's own documented CONC-03/04 precedent of proving the database constraint directly rather than through real HTTP concurrency. HOLD-03 (opaque in availability) and "`GET /bookings` never returns held rows" both turned out to already be true and already covered by Phase 6 tests using an ORM-constructed held row; this phase added COMPLEMENTARY tests proving the SAME properties hold for a hold created through the REAL mechanism this phase built, rather than either claiming Phase 6's coverage as its own or duplicating it without reason. RECON-05's predicate-covers-`'held'` schema-assertion test (`tests/test_schema_assertion.py`) turned out to already exist too — written in Phase 3, ahead of `'held'` having a real writer — verified still passing and its stale "since 'held' rows don't exist until Phase 15" docstring corrected, not silently left inaccurate. Full suite: 207 tests (201 + 6 new), no regressions | Pending (on branch `phase-15-holds-exclusion-domain`) |
| 16 | Offers: Creation, Acceptance, Cascade 🏁 MILESTONE 3 | Completes the waitlist: `waitlist_offer` (Spec v1.0 §3 — `hold_booking` a `OneToOneField` per the `UNIQUE hold_booking_id` column, `uniq_active_offer_per_entry` partial unique index, no EXCLUDE constraint with Spec's own forbidding comment reproduced verbatim in the migration per this phase's explicit Scope IN), `kairos_app` grants + audit trigger (`core/migrations/0009`). `create_offer_for_freed_range` (`kairos/waitlist/services.py`) is the RFC v1.0 §10.2/Spec v1.0 §4.2 worker — walks `find_eligible_entries`' (Phase 14) FCFS-ordered candidates, attempts a hold via `create_booking(status=HELD, actor_type=SYSTEM)` (Phase 15) for each in turn, advancing to the next on `SlotUnavailableError` (Spec's own "re-query and try the next candidate"), and only constructs the `WaitlistOffer` row once a hold has actually committed (PRD FR23: hold before offer, structurally, not by caller discipline). `cancel_booking`'s Phase 7 `on_commit()` log stub now really dispatches `create_offer_for_freed_range_task.delay(...)` (RFC v1.0 §5c step 4) via a NEW `kairos/waitlist/tasks.py` (Celery's `autodiscover_tasks()` module-naming lesson, Phase 13, applied a second time) — a genuine circular-import risk between `kairos.bookings.services` (needs the task) and `kairos.waitlist.services` (needs `create_booking`) is broken with a deferred, call-time-only import inside the task function, verified via `manage.py check` actually succeeding. `POST /waitlist-offers/{id}/confirm` executes `accept_offer` — the literal RFC v1.0 §10.3/Spec v1.0 §4.3 conditional `UPDATE` (status, owner, AND `expires_at > now()` in one statement) — structurally incapable of `slot_unavailable` (no INSERT on this path); 0 rows → 409 `offer_expired`. `POST /waitlist-offers/{id}/decline` releases the hold (`status`→`cancelled`, `expires_at`→`NULL` — the `hold_has_expiry` CHECK requires the latter, a real bug caught building WL-02 and traced back into `decline_offer` itself, fixed in both places) and dispatches the SAME cascade worker for the freed range; unlike every other cancel-shaped endpoint here, an already-resolved offer is a 409 `offer_already_resolved`, not a 200 no-op — Spec's own explicit, deliberate choice. The declining entry's status becomes `EXPIRED` (present in the schema since Phase 14, unused until now) rather than back to `WAITING`, which is what stops it from immediately re-winning the very cascade its own decline triggers. `RecordableConflictError` (new base class in `core/exceptions.py`) consolidates `SlotUnavailableError`/`AlreadyOnWaitlistError`/`OfferExpiredError`/`OfferAlreadyResolvedError` — `kairos_exception_handler` and `run_idempotent_write` each collapsed four near-identical branches into one, justified once the pattern reached its fourth instance, not before. HOLD-01 through Phase 15's tests are joined by: WL-01 (two threads, real `cancel_booking` calls under `transaction=True`, ground truth via `count_overlapping_pairs` — B1/B2 built as adjacent rather than Test Plan's literal overlapping example times, since two overlapping bookings cannot both be `confirmed` simultaneously in the first place, the exact guarantee this project enforces; see Key Technical Decisions), WL-02 (100 barrier-released runs — reaper-expiry simulated as `UPDATE ... SET status='cancelled', expires_at=NULL ...`, split 50/50 between expires-in-the-future and expires-in-the-past to deterministically exercise both orderings rather than gambling on timing jitter; `ClientOutcome` gained an additive `rowcount` field since a conditional UPDATE matching zero rows isn't an error), and WL-03 (cascade reaches entry 2 not 3 after decline; skips a withdrawn entry 2 to reach entry 3). Full suite: 223 tests (207 + 16 new), no regressions | Pending (on branch `phase-16-offers-cascade`) |
| 17 | Dual Reclamation: Reaper & Cleanup-on-Write ⚠️ CRITICAL | Both RFC v1.0 §10.4 mechanisms, because a constraint predicate cannot express expiry and neither alone suffices. Mechanism 1 — cleanup-on-write: a `DELETE FROM booking WHERE resource_id=... AND status='held' AND expires_at<=now() AND time_range && tstzrange(...)`, added inside `create_booking`'s (`kairos/bookings/services.py`) own transaction, immediately before the INSERT, unconditionally for EVERY caller (ordinary booking, recurring occurrence, rolling materialization, the cascade worker's own hold creation) — with the RFC v1.0 §10.1-style load-bearing comment (Implementation Plan §1.3 item 5) at the DELETE's own call site. Mechanism 2 — the reaper: `reap_expired_holds` (`kairos/waitlist/services.py`), scheduled via `CELERY_BEAT_SCHEDULE` at the new `HOLD_REAPER_INTERVAL_SECONDS` (default 30s, `core/constants.py`) — one independent transaction per expired hold (mirroring `confirm_recurring_series`/`rolling_materialize_series`'s per-occurrence isolation), reclaiming each via the IDENTICAL conditional-UPDATE-to-`cancelled` shape `accept_offer` races against, cascading via the SAME `create_offer_for_freed_range` cancellation/decline already use, and writing a `system_check_run` heartbeat (`check_name='hold_reaper'`) every run regardless of findings. `hold_reaper_heartbeat_is_stale` reads that heartbeat back — WL-05 Part B's actual mechanism, scoped honestly to the DATA a future alert would consume (full alert routing and `GET /admin/checks/latest` are Phase 21, not built here). `dispatch_cascade` (new, `kairos/waitlist/tasks.py`) is now the ONE place `create_offer_for_freed_range_task.delay(...)` is called from (`cancel_booking` and `decline_offer` both go through it) — wraps the call in `try/except` so a broker outage degrades (logs `cascade_dispatch_failed_broker_unavailable`) rather than raising out of a `transaction.on_commit()` callback and turning an already-committed, successful cancel/decline into an apparent request failure. RECLAIM-01 (booking succeeds over a stale hold with no reaper running, hold genuinely gone via DELETE — not superseded), RECLAIM-02 (reaper cascades to the next eligible entry with zero booking traffic, proven by calling `reap_expired_holds` directly — Test Plan's own "controllable time" requirement, not a real 30s wait), and RECLAIM-03 (100 barrier-released runs, cleanup-on-write's DELETE+INSERT racing the literal RFC v1.0 §10.3 acceptance UPDATE, correctness inferred by correlating outcomes since a party's own DELETE rowcount isn't directly observable through the harness) all pass. RECLAIM-04 (200 writers × 50 runs, N=200-identical-slot-style contention plus 4 pre-seeded expired holds cleanup-on-write must clear every attempt) was ACTUALLY RUN at full DoD-specified scale, not merely written: **269 real SQLSTATE 40P01 deadlocks occurred, in roughly half of the 50 runs** (some runs: zero; others: 9–16) — not the "zero deadlocks" the DoD's literal text names. Per this phase's own explicit instruction, this is the SECOND of the two anticipated honest outcomes, not a phase failure: safety held on every single one of the 10,000 attempts (never more than one success per round, 50/50), zero unexplained SQLSTATEs, zero rounds even needed the zero-success retry budget, and 40P01 was ALREADY a documented, retryable SQLSTATE `BookingService` treats as 503 (Phase 4) before this phase ever ran — cleanup-on-write's extra DELETE measurably raises deadlock frequency versus CONC-01's own N=200 baseline (empirically ~2/10 runs there), a real, honestly-reported finding, not a design failure requiring cleanup-on-write's removal. RECLAIM-04 is deliberately EXCLUDED from the default `pytest tests/concurrency` CI sweep (`.github/workflows/ci.yml` now `--ignore`s it) — Test Plan v1.0 §13 places it in the staging/pre-release tier, not CI tier, the identical tiering CONC-01's own full-scale escalation already has. WL-05 Part A and WL-06 were verified LIVE against the real `docker compose` stack — genuinely stopping `beat`/`redis` containers (`docker compose stop beat` / `stop redis`), not mocked — per this phase's own explicit instruction that a mocked simulation would not prove what RFC v1.0 §4.3's real degradation behavior requires: Part A seeded a hold expiring in 3s with `beat` stopped, confirmed it sat `status='held'` 12+ seconds past expiry with zero error anywhere in the worker/server logs; WL-06 stopped `redis` and confirmed, over real HTTP against `manage.py runserver` under `kairos.settings.dev` (real, non-eager Celery — `CELERY_TASK_ALWAYS_EAGER` is test-settings-only): booking creation (201) and cancellation (200) both succeeded, cancellation's cascade dispatch failed with a genuine `kombu.exceptions.OperationalError` caught and logged by `dispatch_cascade` rather than crashing the response, a booking over an expired hold succeeded via cleanup-on-write (the stale hold row was gone afterward) with zero `waitlist_offer` rows ever created, and the worker reconnected cleanly once `redis` restarted. WL-05 Part B is covered by ordinary pytest (`hold_reaper_heartbeat_is_stale`); `test_dispatch_cascade.py` is a lightweight automated regression guard for the try/except itself (`CELERY_TASK_ALWAYS_EAGER` means pytest can never reproduce a genuine broker outage — this only protects against someone removing the try/except later). Full suite: 236 tests (223 + 13 new — RECLAIM-04 counted but excluded from the routine sweep), no regressions | Pending (on branch `phase-17-dual-reclamation`) |
| 18 | Notifications | `NotificationService` (`kairos/core/notifications.py`) is a thin wrapper over Django's own `EMAIL_BACKEND` abstraction (console in dev, real SMTP in prod, `locmem` — Django's own capturing backend, `django.core.mail.outbox` — in test) rather than a bespoke backend hierarchy — reusing a framework guarantee instead of reinventing one, the same choice this project already made for `CompositePrimaryKey`/`set_config`/Postgres triggers. Every `notify_*` builder function only constructs a subject/body and calls `kairos/core/tasks.py`'s new `dispatch_notification`, which enqueues `send_notification_task.delay(...)` and returns immediately — the actual `send_mail()` call happens inside that Celery task, in a worker process, NEVER inline in an HTTP request thread or inside a request-path transaction (RFC v1.0 §15a). `dispatch_notification` mirrors `kairos.waitlist.tasks.dispatch_cascade` (Phase 17) exactly: wraps `.delay()` in `try/except` so a broker outage degrades (logged `notification_dispatch_failed_broker_unavailable`) rather than propagating out of `cancel_booking`'s `transaction.on_commit()` callback. `send_notification_task` is `autoretry_for=(Exception,)`, `retry_backoff=True`, `max_retries=NOTIFICATION_MAX_RETRIES` (PRD FR55's "recorded and retried") — the delivery logic itself lives in a plain function, `_execute_notification_delivery`, callable directly so a test can drive a failure-then-success sequence without depending on Celery's own retry scheduling/timing, the same "test the mechanism directly" precedent `expand_occurrences`/`reap_expired_holds` already established. New schema, `NotificationLog` (`kairos/core/models.py`, migrations 0010-0011) — Spec v1.0 §3 has zero notification concept at all, the identical kind of gap Phase 9's `user_group` table filled — one row per logical notification, `attempts` accumulating and `status` (`pending`/`sent`/`failed`) reflecting the latest outcome across every retry; `kairos_app` granted SELECT/INSERT/UPDATE (no DELETE, no audit trigger — this table is itself a delivery-outcome log, the same category as `system_check_run`, not one of the five audited business-state entities). Four of the five notification points wired to real callers: `notify_offer_created` (PRD FR52, expiry stated explicitly in the subject line) from `create_offer_for_freed_range` right after the offer row commits; `notify_admin_cancellation` (PRD FR53, includes the recorded reason) from `cancel_booking`'s SECOND `on_commit` hook, gated on `actor_type=ADMIN` so a self-cancel never triggers it; `notify_rematerialization` (PRD FR54) from `rematerialize_stale_series` on each successfully-recomputed occurrence; and `notify_rematerialization_conflict` (PRD FR13b, a genuine completion of Phase 13's own "resource_administrators: pending Phase 18 delivery" placeholder, not new scope) to both the series owner and every resource administrator scoped to that specific resource on a conflict. The fifth, `notify_rollback_hold_released` (Rollout v1.0 §4.5), was built per this phase's own explicit clarification: the template/message content and send mechanism exist and are fully tested standalone (distinct, non-generic wording from an ordinary offer-expiry notification — explicitly names the rollback, explicitly states queue position was preserved, deliberately avoids any "expire" framing), but NO real production trigger was invented for it — Rollout §4.5's hold release is a manual operational runbook (SQL an operator runs during an incident), not application code any phase has built, and manufacturing a fake caller just to wire one up would have been scope beyond this phase's actual job (see Open Questions). Definition of Done verified: all four wired notification points fire, proven via the capturing (`locmem`) backend; offer-created states the expiry explicitly; a simulated provider outage (patching `send_notification_task.delay` to raise) does NOT fail the underlying admin-cancellation — the booking still commits `CANCELLED`, proven directly; every dispatch call site is either inside a `transaction.on_commit()` callback or a worker context with no open transaction — verified by reading the code, no synchronous dispatch from a request-path transaction anywhere; rollback-hold-released messaging asserted distinct from ordinary offer-created messaging by direct content comparison. Full suite: 238 tests (225 + 13 new), no regressions, plus all 10 CI-tier concurrency tests re-run clean (this phase's changes touch `cancel_booking`, a CONC/WL/RECLAIM-exercised function). ⚠️ **Real bug caught by rebuilding the worker's Docker image and re-reading its startup banner** — `send_notification_task` was silently absent from the running worker's task list after a plain `docker compose restart` (which reuses the existing, stale image rather than rebuilding); fixed with `--build`, then re-verified live: the rebuilt worker's banner lists all six tasks, a deliberately malformed dispatch demonstrated genuine exponential retry-with-backoff (1s/0s/4s/2s/6s, jittered) against the real Redis broker before failing cleanly once `NOTIFICATION_MAX_RETRIES` was exhausted, and a valid dispatch printed the full email to the worker's console `EMAIL_BACKEND` and left exactly one `notification_log` row (`status='sent'`, `attempts=1`), confirmed via `psql` against `kairos_dev` — see Key Technical Decisions | Pending (on branch `phase-18-notifications`) |

## Current Phase In Progress

None. Phase 18 is complete pending review and merge. Phase 19 (Resource Administration &
Offboarding) is next.

## NOT Yet Built

No frontend. `held` rows exist as of Phase 15 and genuinely occupy the exclusion domain
(HOLD-01/02); Phase 16 gave them a real caller (cancellation and offer-decline both create/
release holds via the actual cascade worker); Phase 17 makes an UNANSWERED hold stop blocking
anything too — both reclamation mechanisms (RFC v1.0 §10.4) are real now: cleanup-on-write
(inside every `create_booking` call) and the reaper (`reap_expired_holds`, on Celery Beat
every `HOLD_REAPER_INTERVAL_SECONDS`). `waitlist_entry` (Phase 14) and `waitlist_offer`
(Phase 16) both exist and are both real: `create_offer_for_freed_range` (Phase 16) is
`find_eligible_entries`' (Phase 14) first real caller, dispatched after a cancellation, an
offer decline, OR now an expired-hold reclamation (Phase 17) — every trigger RFC v1.0 §10.2/
§10.4 describes now has a real caller, AND (Phase 18) each of those events now actually
notifies someone: offer creation dispatches `notify_offer_created` with the expiry stated
explicitly (PRD FR52). Full alert ROUTING (Phase 21) is separately still missing —
`hold_reaper_heartbeat_is_stale` (Phase 17) makes staleness DETECTABLE from the data, and
`GET /api/v1/admin/checks/latest` (the surface WL-05 Part B's own text names) doesn't exist
yet to expose it. Celery/Redis exist as of Phase 13 (`kairos/celery.py`, `infra/docker-
compose.yml`'s `redis`/`worker`/`beat` services); registered tasks are now
`rolling_materialize_series_task`/`rematerialize_stale_series_task`/`check_tzdata_drift_task`/
`create_offer_for_freed_range_task` (Phase 16)/`reap_expired_holds_task` (Phase 17)/
`send_notification_task` (Phase 18). `recurring_series` rows are created for
real via
`POST /api/v1/bookings/recurring`
(Phase 12), and the ROLLING MATERIALIZATION MECHANISM now exists (Phase 13,
`kairos/bookings/tasks.py`) — but Phase 12's confirm STILL rejects a series whose occurrences
extend beyond the 365-day horizon outright at 400 (REC-06 still passes, unmodified), rather
than materializing part of it now and leaving the rest for this job per PRD FR14c. This means
the rolling-materialization job currently has NO REAL SERIES to act on — it's proven directly
against series constructed at whatever `materialized_through` a future confirm revision would
leave them at (tests/bookings/test_rematerialization.py), the same "mechanism before its real
caller" situation `actor_type='system'` itself was in from Phase 8 until this same phase gave
it one. Whichever future phase revisits Phase 12's horizon-rejection to actually produce a
partially-materialized series should treat this job as already built, not build a second one —
see Open Questions. tzdata re-materialization (RFC §9.4) DOES have a real, if synthetic-in-
tests, target: any `RecurringSeries` whose `tzdata_version` differs from what's installed.
Notification DELIVERY for PRD FR54 (re-materialization) and PRD FR52 (a waitlist offer's
explicit expiry) is real now (Phase 18) — `rematerialize_stale_series` dispatches
`notify_rematerialization` on each successfully-recomputed occurrence and `notify_
rematerialization_conflict` to the series owner and every resource administrator on a
conflict; `create_offer_for_freed_range` dispatches `notify_offer_created` right after the
offer commits. A fifth notification type, `notify_rollback_hold_released` (Rollout v1.0
§4.5), is built and independently tested but has NO real production caller — Rollout §4.5's
hold release is a manual operational runbook, not application code any phase automates; see
Open Questions. Full six-check monitoring/alerting/
heartbeats (RFC §14) is Phase 21; Phase 13 only WRITES `system_check_run` rows, it doesn't yet
alert on staleness or absence of a run. No replica routing (Phase 30 — `data_freshness` is
hardcoded `"primary"`, always true today since no replica exists). The audit trail covers
`booking`/`resource`/`resource_admin`/`waitlist_entry`/`waitlist_offer` (Phase 16 adds the
last one) — `actor_type='unknown'` alerting (as opposed to just recording the row) is Phase
21. AUD-03(d)'s "system-initiated write" now has TWO real workers to exercise it (`actor_type=
'system'`, Phase 13's rolling materialization — proven via a spy-on-cursor test reading the
session variable directly — AND Phase 16's hold creation via `create_offer_for_freed_range`,
proven against the PERSISTED `audit_log` row instead, since `create_booking` already reuses
the identical session-settings mechanism Phase 13's spy test verified once). Resource CRUD
(create/update/admin
grants) is Phase 19 — the Phase 6 resource endpoints are read-only, and no live code path
writes `resource_admin` yet (`ResourceAdmin` rows in tests are created directly via the ORM,
not through any endpoint). IDEM-07/08
(fault injection — process kill mid-transaction, proxy-level response drop) need tooling that
arrives in Phase 28; idempotency coverage on waitlist join/offer confirm arrives with those
endpoints (Phases 14, 16); `run_idempotent_recurring_confirm`'s own crash-mid-series gap
(Phase 12 — key stays `in_progress` forever if the process dies between claiming it and
recording the outcome, with no automatic recovery beyond the existing 24h cleanup command) is
the same class of documented-not-solved gap, specific to this endpoint. Editing a recurring
series definition (PRD FR16) has no endpoint — Phase 12's Scope IN only ever included preview/
confirm/cancel, never edit, despite FR16 appearing on the phase's "Documents satisfied" line;
the CANCEL endpoint honors FR16's underlying "past occurrences are historical and immutable"
principle for the one operation Phase 12 actually built, but series editing itself remains
unbuilt — see Key Technical Decisions/Open Questions. Recurring-series preview/confirm also
validates NEITHER the resource's `bookable_start_time`/`bookable_end_time`/
`max_booking_duration_minutes` NOR series-start-date past-dating — Spec v1.0 §5.8's own
failure table never lists either as a 400 cause (unlike Spec v1.0 §5.1's single-booking
create, which explicitly does), and — unlike the reason-on-cancel gap below — doing either
CORRECTLY is not actually small: bookable hours are defined in the RESOURCE's own timezone,
which can differ from a series' own `timezone` field, so a bookable-hours check done right
means converting each occurrence's UTC instant into `resource.timezone` and checking
per-occurrence (the resource's own DST can shift which occurrences pass independently of the
series' DST), not one series-level comparison of raw local times — a real design question
(also: does a bookable-hours violation reject the whole preview, or surface per-occurrence
like a conflict?) Spec doesn't answer. Flagged, not invented — see Open Questions for which
phase should resolve it. A series can currently be previewed/confirmed entirely in the past.
`booking.series_id` is populated for real
now (Phase 12) for every occurrence `POST /api/v1/bookings/recurring` creates — still NULL
for every one-off booking, by design. No `booking` column records the IANA zone a
one-off booking was created under (PRD FR7's second sentence) — Spec v1.0 §3 never defined
one, and Phase 10 flagged rather than invented one (see Key Technical Decisions/Open
Questions); whichever future phase needs it for display/audit should add it deliberately
rather than assume it already exists.
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
| `expand_occurrences` computes each occurrence's DATE via plain `date + timedelta(days=7*i)` arithmetic, then converts ONLY the resulting local wall-clock time to UTC — date arithmetic never touches a UTC instant at any point | Date arithmetic (adding whole days) is DST-agnostic by construction — a date has no time-of-day component to drift. The RFC v1.0 §9.1 bug is specifically about accumulating a fixed-DURATION offset (`7*24h`) on a UTC INSTANT; stepping the DATE instead and converting each result independently sidesteps the bug class entirely rather than correcting for it after the fact | `kairos/bookings/recurrence.py` (`expand_occurrences`) |
| The nonexistent-time transition gap is COMPUTED per-occurrence (`fold=0` instant minus `fold=1` instant at that exact wall-clock value), never hardcoded to 1 hour | Most real-world DST transitions are 1 hour, but `zoneinfo`'s data isn't guaranteed to be — a hardcoded constant would be correct by coincidence for every zone this project's own tests happen to check and silently wrong for one it doesn't. Computing it directly from the two candidate offsets is the same "derive from the real IANA data, never assume" discipline Phase 10's detection helpers already established | `kairos/bookings/recurrence.py` (`_transition_gap`) |
| A nonexistent occurrence shifts BOTH its start and end by the same gap, preserving local duration — not just the start | RFC v1.0 §9.3 / PRD FR11 says "shift the occurrence forward," not "shift the start forward." Shifting only the start while leaving the end fixed would silently change how long the booking lasts, which is a second, undisclosed side effect beyond the one FR11 already requires disclosing | `kairos/bookings/recurrence.py` (`_materialize_occurrence`) |
| An ambiguous occurrence needs NO shift at all — only disclosure. `local_to_instant`'s `fold=0` default already resolves to the first (pre-transition) instant, the exact policy PRD FR12 asks for | Verified directly, not assumed: for Paris's 2027-10-31 fall-back, `fold=0` produces `00:30Z` (earlier) and `fold=1` produces `01:30Z` (later) — `fold=0` is chronologically first by construction, so the "normal" code path Phase 10 already built for non-transitional times happens to already implement FR12's policy for free once the ambiguous case is merely detected | `kairos/bookings/recurrence.py`, `kairos/core/timezones.py` (`is_ambiguous_local_time`) |
| `occurrence_count` bounds (PRD FR14a) are checked in `expand_occurrences` itself via `PolicyValidationError`, in addition to `RecurringSeries`'s DB `CHECK` constraint | Spec v1.0 §5.8's preview endpoint (Phase 12) "commits nothing" — no `RecurringSeries` row is ever created during preview, so a DB-only CHECK constraint would never fire for the one caller most likely to send `occurrence_count=101` in the first place. The Python-level guard in the pure engine covers preview; the DB CHECK remains as a backstop against any bulk/raw-SQL write that bypasses the engine entirely | `kairos/bookings/recurrence.py`, `kairos/bookings/migrations/0003_recurring_series.py` |
| `idx_series_materialized_through` is created via a raw-SQL migration, not a Django `models.Index`, despite Spec v1.0 §3 specifying it as an ordinary (non-load-bearing) index | The name is 32 characters — past Django's 30-character `models.Index` name limit (`models.E034`), a portability convention for databases with tighter identifier limits than Postgres's actual 63. Shortening the name would diverge from the Spec's literal DDL for no functional reason; raw SQL reproduces it exactly, the same tool (not the same *reason*) Phase 2 used for the exclusion constraint | `kairos/bookings/migrations/0004_series_materialized_through_index.py` |
| `RecurringSeries.save()` reuses Phase 10's `validate_iana_zone` unconditionally, exactly like `Resource.save()` | Same field, same requirement (PRD FR8), same "no write path yet, so validate at the one place all writes — including test fixtures and Phase 12's future service layer — actually go through" reasoning Phase 10 already established for `Resource`. Not a new decision, a consistent application of an existing one | `kairos/bookings/models.py` (`RecurringSeries.save`) |
| `core/migrations/0005_kairos_app_recurring_series_grants.py` is a NEW migration, not an edit to Phase 8's `0003` or Phase 9's `0004` | GRANT statements aren't retroactive — a table created after an earlier grants migration ran isn't covered by it. This is the third time this exact pattern has been needed (Phase 8's original grants, Phase 9's `user_group`/`user_group_membership`, now Phase 11's `recurring_series`) — verified live by connecting AS `kairos_app` and both `SELECT`ing and `INSERT`ing (rolled back) against `recurring_series`, not just by reading the migration | `kairos/core/migrations/0005_kairos_app_recurring_series_grants.py` |
| `BookingResponseSerializer.get_series_id`'s Phase-9-era stub (`return None`, commented "doesn't exist until Phase 11") is wired up to the real `booking.series_id` column now that it exists, rather than left as `None` for a later phase to remember | The stub's own comment named Phase 11 explicitly as when this should happen — leaving it unwired after adding the column it was waiting on would recreate exactly the kind of "described but not actually done" gap Phase 7's session-settings regression already taught this project to distrust (see that Key Technical Decisions row) | `kairos/bookings/serializers.py` (`BookingResponseSerializer.get_series_id`) |
| The `preview_token` is a signed, self-contained HS256 JWT (the whole series definition plus computed conflict/adjustment date sets), not a server-side preview table | Spec v1.0 §5.8 is explicit: preview "commits nothing." A DB-backed preview table would still be a write, even if not to `booking` — REC-01's ground-truth assertion is specifically about `booking` rows, but the stated intent ("commits nothing") reads more broadly. A signed token needs zero server-side state, expires via its own `exp` claim (REC-04 is then just ordinary JWT verification, not custom expiry bookkeeping), and reuses the EXACT signing key (`KAIROS_SESSION_SIGNING_KEY`) the Phase 9 session token already established this pattern with — not a third signing key for a third short-lived internal token type | `kairos/bookings/recurring_series.py` (`_issue_preview_token`, `decode_preview_token`) |
| `confirm_recurring_series` re-runs `expand_occurrences` with the exact inputs decoded from the token, rather than trusting any occurrence data the token itself might carry | This is what makes REC-07 ("preview and confirm use the same expansion code path") true BY CONSTRUCTION — one function, called twice with identical arguments — rather than by two independent implementations happening to agree. Verified live, not just in a test: a real preview→confirm `curl` round trip produced byte-identical UTC instants for TZ-01's exact DST-spanning series | `kairos/bookings/recurring_series.py` (`confirm_recurring_series`) |
| Confirm attempts ONLY the occurrences the preview did NOT already know conflicted — an already-known conflict is reported as `acknowledged: true` directly, with no INSERT attempt at all | The user's acknowledgment of a known conflict IS their agreement that occurrence will not be created — retrying it anyway would be attempting a write the user was never asked to confirm, and would make `acknowledged: true` vs `false` (REC-03's whole point) ambiguous: a retried-and-still-failed known conflict would look identical to a newly-arisen one unless a separate flag were invented. Not retrying keeps the acknowledged/unacknowledged distinction exactly as simple as REC-03 needs it | `kairos/bookings/recurring_series.py` (`confirm_recurring_series`) |
| Confirm's per-occurrence writes reuse `create_booking` (Phase 4) completely unchanged — no new "create one occurrence" function | This is what makes RFC v1.0 §5d's "each occurrence in its own transaction" and Phase 12's explicit "each one needs its OWN correctly-timed session-settings application" instruction true for free: `create_booking` already opens a fresh `transaction.atomic()` and calls `apply_write_path_session_settings` on every invocation. Calling it N times in a loop gives N independent transactions and N independent session-settings applications without Phase 12 reimplementing either. Proven by REC-05 (occurrence 6 survives occurrence 7's real, induced conflict — impossible if they shared a transaction, since any statement error aborts the WHOLE transaction absent a savepoint) plus a dedicated test confirming one correctly-attributed `audit_log` row per occurrence. Distinct `occurred_at` timestamps were originally cited here too as further proof — corrected in Phase 13 after verifying directly that Django's `Now()` compiles to `statement_timestamp()`, which varies per SQL statement regardless of transaction grouping, so it isn't actually evidence of separate transactions; REC-05 was always the real proof | `kairos/bookings/services.py` (`create_booking`, extended with an optional `series` field), `kairos/bookings/recurring_series.py` (`confirm_recurring_series`) |
| `run_idempotent_recurring_confirm` is a NEW function in `core/idempotency.py`, not a parameterized variant of `run_idempotent_write` | `run_idempotent_write`'s entire design rests on the key claim and "the write" sharing ONE transaction (see that function's own docstring) — structurally impossible here, since there are N writes, each needing its OWN transaction per RFC v1.0 §5d. The key is claimed and COMMITTED first, occurrences are attempted, and the outcome is recorded in a THIRD transaction after — which means, unlike `run_idempotent_write`, a concurrent replay CAN observe a genuinely COMMITTED `in_progress` row (not just lock contention), so a dedicated `_replay_or_conflict_allowing_in_progress` handles that case explicitly. Documented, deliberate, NOT covered by any Phase 12 test: a crash between the key-claim commit and the outcome-record leaves the key permanently `in_progress`, with recovery only via the existing 24h cleanup command — after which a retry could re-create already-created occurrences. Flagged the same way IDEM-07/08's fault-injection gaps are flagged elsewhere in this project, not silently assumed solved | `kairos/core/idempotency.py` (`run_idempotent_recurring_confirm`) |
| `POST /recurring-series/{id}/cancel` reuses the EXISTING `run_idempotent_write` (single shared transaction), not the new `run_idempotent_recurring_confirm` | Cancellation can never lose to the exclusion constraint — moving rows OUT of `status IN ('confirmed','held')` never conflicts with anything — so there is no "one contested occurrence" isolation problem the way CREATE has, and RFC v1.0 §5d's per-occurrence-transaction requirement was written specifically for creation, not cancellation. A single bulk `Booking.objects.filter(id__in=...).update(...)` is correct and simpler; Postgres's row-level audit trigger still fires once per affected row regardless of statement count, so the audit trail is identical either way | `kairos/bookings/services.py` (`cancel_recurring_series`) |
| REC-06's `occurrence_count=100` boundary test starts its series 350 days in the PAST, not from "now" | 100 WEEKLY occurrences always span 99×7 = 693 days end to end — no start date from "now" forward keeps all 100 within the SEPARATE 365-day horizon bound REC-06 also tests. This isn't a workaround for a bug; it's what isolating two genuinely independent bounds in one test requires, since this endpoint has no past-dating rejection to prevent it (see the gap noted in NOT Yet Built) | `tests/bookings/test_recurring_series.py` |
| Recurring-series preview/confirm validates NEITHER the resource's bookable-hours/max-duration policy NOR series-start-date past-dating | Spec v1.0 §5.8's own 400-cause list ("invalid weekday/timezone; local_end_time <= local_start_time; occurrence_count outside 1-100; horizon exceeded") never mentions either, unlike Spec v1.0 §5.1's single-booking create, which explicitly checks both via `_validate_range_policy`. Explicitly NOT a "just add it, it's small" case: bookable hours are defined in the resource's OWN timezone, which can differ from the series' `timezone` field, so a correct check is per-occurrence (converting each occurrence's UTC instant into `resource.timezone`) and raises an unanswered behavioral question (reject the whole preview, or report per-occurrence like a conflict, per FR10?) Spec doesn't resolve — genuinely deferred, recommended to Phase 13, not silently invented under time pressure | Spec v1.0 §5.8 (no such checks listed); `kairos/bookings/serializers.py` (`RecurringSeriesPreviewSerializer`); see Open Questions |
| `POST /recurring-series/{id}/cancel` DOES require a `reason` on a resource-admin override — added after initial Phase 12 review, not left as a gap | Unlike the bookable-hours/past-dating question above, this one has a DIRECT, unconditional PRD requirement governing it: FR47, "Administrative override of another user's booking requires a recorded reason" — no carve-out for series vs. single bookings. Spec v1.0 §5.10's own example body simply not showing a `reason` field reads as an incomplete example, not a deliberate exemption, given FR47's unconditional wording. Mirrors `BookingCancelSerializer`'s exact pattern (`RecurringSeriesCancelSerializer`, same validation, same error shape) rather than inventing a new one | `kairos/bookings/serializers.py` (`RecurringSeriesCancelSerializer`), `kairos/bookings/services.py` (`cancel_recurring_series`) |
| `create_booking`/`edit_booking` gained an `actor_type` field (default `USER`) rather than a bespoke system-write function | Confirm (Phase 12) already proved reusing the identical function per-occurrence is what gives every write its own transaction and fresh session settings for free — a background job (Phase 13) needing `actor_type='system'` is the SAME property, not a different one, so extending the existing function (backward-compatible default, every prior caller unchanged) was the consistent choice over writing `create_system_booking`/`edit_system_booking` | `kairos/bookings/services.py` |
| `actor_id` is passed as `""` (not `req.user.id`) specifically when `actor_type == SYSTEM` | Mirrors the EXACT convention `apply_write_path_session_settings` already established for an absent `reason` — empty string becomes SQL NULL via the trigger's `NULLIF(current_setting(...), '')`. A background job genuinely has no human actor; `req.user`/`req.booking.user_id` in these requests only says who the booking is FOR, which for a system write is still meaningful (the series' `created_by`) even though it isn't who ACTED | `kairos/bookings/services.py` (`create_booking`, `edit_booking`) |
| `actor_type='system'` is proven with the SAME spy-on-cursor style Phase 7/8/9 already established for session-variable checks — `current_setting('app.actor_type'/'app.actor_id', true)` read immediately before the real write — not inferred from anything indirect | Phase 13's own explicit instruction: "Add a spy test proving app.actor_type='system' is actually visible at the point of the re-materialization INSERT — same verification style as every previous phase's session-variable checks." This is also WHY the distinct-`occurred_at`-timestamps approach Phase 12 used for a related claim was wrong (see the correction below) — an indirect signal, however plausible-sounding, isn't the same as reading the actual session variable | `tests/bookings/test_rematerialization.py` (`test_rolling_materialization_writes_with_actor_type_system`, `test_rematerialization_writes_with_actor_type_system`) |
| ⚠️ **Correction to Phase 12's own claims, found while building Phase 13's spy test**: Phase 12's audit test and two CLAUDE.md entries claimed distinct `occurred_at` timestamps on created bookings' audit rows proved independent TRANSACTIONS | Verified directly against a real connection (separate `cursor.execute()` calls within one uncommitted transaction): Django's `Now()` compiles to Postgres's `statement_timestamp()`, which advances on every STATEMENT regardless of transaction boundaries — N INSERTs inside ONE shared transaction would ALSO produce N distinct timestamps. The claim was never actually evidence of what it was cited for. REC-05 (occurrence 6 survives occurrence 7's real, induced conflict — impossible if they shared a transaction, since Postgres aborts the WHOLE transaction on any statement error absent a savepoint) was always the real, airtight proof and was never in question — only the SECOND, redundant "proof" was wrong. All three Phase 12 citations (the Completed Phases row, its own Key Technical Decisions row, and the test's docstring) are corrected, not silently left standing | `tests/bookings/test_recurring_series.py` (`test_confirm_writes_one_audit_row_per_created_occurrence`, renamed from `..._with_independent_commit_times`) |
| Re-materialization matches an existing `Booking` to its recomputed occurrence by NEAREST instant (within a 2-day tolerance), not by converting the stored (possibly wrong) instant back to a local date | The entire premise of re-materialization is that the stored instant may be WRONG — computed under rules that no longer apply — so deriving "which occurrence is this" from that same wrong value is circular and can be misleading exactly where correctness matters most. Occurrences are 7 days apart; any plausible DST shift is under 24h; "closest recomputed occurrence, within 2 days" is unambiguous without needing to reconstruct anything from stale data | `kairos/bookings/tasks.py` (`rematerialize_stale_series`) |
| A series with an UNRESOLVED re-materialization conflict keeps its OLD `tzdata_version` — not bumped to current even though the series WAS checked | "Checked against current tzdata" and "every occurrence successfully corrected" are different claims; bumping the version on partial success would make the series look resolved when Test Plan TZ-08's conflicted occurrence is still sitting at its wrong, un-notified instant. Leaving it stale means the NEXT scheduled run retries exactly the occurrences that didn't yet succeed — free, correct retry behavior, not an oversight | `kairos/bookings/tasks.py` (`rematerialize_stale_series`) |
| TZ-03 Test B's drift check (installed vs. latest-on-PyPI) does NOT write to `system_check_run`, unlike rolling materialization / tzdata re-materialization | Spec v1.0 §3's `check_name` CHECK constraint lists exactly six values, and none of them mean "the installed version itself is behind what's published upstream" — that's a genuinely different concern from `tzdata_rematerialization` (internal: does this SERIES' recorded version match what's installed). Reusing that slot for a different meaning, or inventing a 7th CHECK value the Spec's DDL doesn't define, were both rejected; logging is the alerting mechanism for now, matching RFC v1.0 §14's own scoping of full monitoring to Phase 21 | `kairos/core/tzdata_check.py` |
| `fetch_latest_tzdata_version` was verified LIVE against the real `https://pypi.org/pypi/tzdata/json` endpoint during this phase, not left at "structurally complete, untested" like the OIDC JWKS path it was modeled after | This environment had outbound network access, so there was no reason to leave a genuinely verifiable claim hedged as unverified — the live call returned "2026.3," the exact version already pinned. The test suite still mocks it (determinism, and to exercise behind/unreachable branches on demand), not because the live path is in doubt | `kairos/core/tzdata_check.py`, verified via a direct one-off call during Phase 13 |
| ⚠️ **Real bug caught only by running `docker compose up` and reading the worker's own startup banner**: `check_tzdata_drift_task` was originally defined inside `tzdata_check.py` directly | Celery's `autodiscover_tasks()` (kairos/celery.py) only scans a module literally named `tasks.py` per installed app — `tzdata_check.py` doesn't match that convention, so the task was silently absent from the worker's registered task list. No error anywhere; `docker compose up` succeeded, the worker started cleanly, and the task simply wasn't there — exactly the "no errors, just absence" failure mode RFC v1.0 §14 warns background jobs are prone to. Caught by reading the worker's `[tasks]` startup banner, not by any automated check. Fixed by moving a thin `@shared_task` wrapper into a NEW `kairos/core/tasks.py`, keeping the real logic in `tzdata_check.py` — then re-verified live: rebuilt the image, confirmed all three tasks now appear in the banner, and dispatched two of them via `.delay()` against the real running worker (one making a genuine PyPI call, one a genuine `kairos_app` Postgres write) to confirm actual execution, not just registration | `kairos/core/tasks.py`, `kairos/core/tzdata_check.py` |
| Rolling materialization and tzdata re-materialization are proven directly against `RecurringSeries`/`Booking` rows constructed via the ORM, not against any output of the real `POST /api/v1/bookings/recurring` endpoint | Phase 12's confirm still rejects a series whose occurrences extend beyond the 365-day horizon outright at 400 (Test Plan REC-06 — unmodified, still passing) rather than materializing part of it now, so NO real caller produces a partially-materialized series today. Changing that behavior was explicitly out of scope for Phase 13 (not in its Scope IN, and would have broken REC-06 — a real, already-passing, spec-literal test). The same "mechanism proven ahead of its real caller" pattern already used for `actor_type='system'` itself (Phase 8 to Phase 13) | `kairos/bookings/tasks.py`, `tests/bookings/test_rematerialization.py`; see Open Questions for which future phase should connect the two |
| A single `Dockerfile` builds BOTH the `worker` and `beat` docker-compose services (different `command:` per service), not two separate images | They share the identical dependency set and codebase — the only difference is the Celery subcommand (`worker` vs. `beat`), which docker-compose's per-service `command:` override already expresses without needing two Dockerfiles to keep in sync | `backend/Dockerfile`, `infra/docker-compose.yml` |
| `waitlist_entry` lives in its own Django app (`kairos.waitlist`), not inside `kairos.bookings` | Spec v1.0 §2's ER diagram and RFC v1.0 §4.2's architecture diagram both reach `waitlist_entry` directly from `app_user`/`resource` — never through `booking` — treating it as a peer entity, not a booking sub-concept. `recurring_series` lived inside `bookings/` because it's the authoritative definition BOOKINGS are materialized FROM (Phase 11's own reasoning); no equivalent relationship holds here | `kairos/waitlist/` |
| `uniq_live_waitlist_per_user_slot` is added via a dedicated raw-SQL migration, not `UniqueConstraint` | The name is 32 characters — past Django's 30-character portability limit (models.E033), the identical situation Phase 11's `idx_series_materialized_through` already established the precedent for (Postgres allows 63; kept verbatim rather than shortened). `idx_waitlist_entry_lookup`/`idx_waitlist_entry_order` are both short enough and stay ordinary `Meta.indexes` (`GistIndex`/`models.Index` with `condition=`) | `kairos/waitlist/migrations/0002_uniq_live_waitlist_per_user_slot.py` |
| `WaitlistEntry.joined_at` uses `db_default=Now()`, not `auto_now_add` | The same fix Phase 8 needed for `AuditLog.occurred_at` (`auto_now_add` is Python-side only) — but here it's also the actual MECHANISM behind SEC-03(a), not just a timestamp-accuracy nicety: a genuine column-level DEFAULT means `WaitlistJoinSerializer` doesn't merely choose to ignore a client-supplied `joined_at`, there is no field for one to bind to at all, so no future write path (including a bulk one that bypasses the serializer entirely) can forge queue position (RFC v1.0 §8.2) | `kairos/waitlist/models.py` |
| `slot_already_available` (422) is checked in `WaitlistJoinSerializer.validate()`, BEFORE the idempotency key is claimed | Mirrors `BookingCreateSerializer`'s established "policy validation before key-claim" precedent (Phase 4): a request that can never succeed (the range is already fully bookable) shouldn't consume a key slot. It's computed via range OVERLAP against confirmed/held bookings (any conflict at all means a direct booking would fail right now, so joining the waitlist is legitimate) — deliberately NOT the same query shape as `find_eligible_entries`'s containment check, which answers a different question (is THIS entry eligible for a FREED range) | `kairos/waitlist/serializers.py`, `kairos/waitlist/services.py` (`slot_is_free`) |
| `kairos.core.idempotency._record_conflict_outcome` was generalized to accept `code`/`message`/`http_status` instead of hardcoding `slot_unavailable` | That module's own docstring already named Phase 14 as a future reuser of `run_idempotent_write` before this phase existed. Spec v1.0 §7 point 7 ("conflict outcomes are recorded too") is a GENERAL idempotency rule, not one scoped to booking conflicts specifically — `already_on_waitlist` needed the identical treatment, and generalizing the one existing function (backward-compatible; the booking-create call site passes the same code/message it always returned) was the consistent choice over a second, parallel recording mechanism | `kairos/core/idempotency.py` |
| `POST /waitlist-entries/{id}/cancel` exists despite Spec v1.0 §5.11/§5.12 documenting only join and list | Implementation Plan Phase 14's own Definition of Done literally states "join, list, and cancel a waitlist entry all work" — the same kind of phase-DoD-beyond-Spec's-literal-endpoint-list situation Phase 12 resolved for the reason-on-series-cancel requirement (FR47). Built on `BookingCancelView`'s exact owner-only, idempotent-double-cancel-is-a-200-no-op shape, MINUS the admin-override branch: Spec never describes one for a waitlist entry, and PRD names no FR requiring it | `kairos/waitlist/views.py` (`WaitlistEntryCancelView`) |
| `find_eligible_entries` (the `@>` containment query, PRD FR21 — this phase's designated load-bearing comment site, Implementation Plan §1.3 item 4) has no live caller yet | Offer creation is Phase 16's job (Scope — DEFERRED in this phase's own instructions: "Offers and cascade → Phase 16. No offer is created in this phase.") — proven directly against ORM-created `WaitlistEntry` rows (WL-04), the identical "mechanism before its real caller" pattern already used for `actor_type='system'` (built in Phase 8, given its first real writer in Phase 13) | `kairos/waitlist/services.py`, `tests/waitlist/test_eligibility.py` |
| A hold is created via `create_booking(..., status=HELD)` — a new `status` field on the existing `BookingCreateRequest`/`create_booking` — not a bespoke `create_hold()` function | The INSERT machinery (session settings, SQLSTATE translation, `refresh_from_db`) is IDENTICAL for a hold and a confirmed booking; only `status`/`expires_at`/who `user` represents differ. The same "one write path, one correctness proof" reasoning Phase 13 already used to add `actor_type` rather than writing `create_system_booking` — applied a second time, to the same function, for a second orthogonal axis | `kairos/bookings/services.py` (`BookingCreateRequest.status`, `create_booking`) |
| `expires_at` is computed INSIDE `create_booking` from `OFFER_WINDOW_MINUTES` whenever `status == HELD`, never passed in by the caller | Keeps the `hold_has_expiry` DB CHECK constraint's invariant (`status='held' ⟺ expires_at NOT NULL`) enforced in exactly one place — a future caller (Phase 16's cascade worker) cannot forget to set it or set it inconsistently, because there is no parameter for it to get wrong | `kairos/bookings/services.py`, `kairos/core/constants.py` (`OFFER_WINDOW_MINUTES`) |
| The RFC v1.0 §10.1 load-bearing comment lives at the `Booking.objects.create(...)` call site inside `create_booking`, in ADDITION to (not instead of) the existing comment at the constraint's own definition (`bookings/migrations/0002_exclusion_constraint.py`) | Implementation Plan §1.3 names "the hold status in the exclusion domain" as its own load-bearing comment site, distinct from Phase 2's constraint-definition one — a developer editing the WRITE PATH (this function) and a developer editing the SCHEMA (that migration) are different people at different times; each needs the warning at the point THEY are looking, not only at the other one | `kairos/bookings/services.py` |
| HOLD-02 (50 barrier-released `POST /bookings` attempts, per Test Plan v1.0's literal wording) is implemented as raw psycopg INSERTs through `tests/concurrency/harness.py`, not real HTTP requests | Consistent with this codebase's own established precedent: CLAUDE.md already documents CONC-03/04 making the identical choice for "edit," a verb that also conceptually maps to an HTTP endpoint. What HOLD-02 proves — the EXCLUDE constraint's `status IN ('confirmed','held')` predicate stops every concurrent writer — is a database-level guarantee independent of which code path issues the INSERT; the application-level translation (23P01→409) is separately proven by `test_hold_01_ordinary_booking_loses_to_outstanding_offer`'s real HTTP step. Also sidesteps a real mechanical problem real HTTP concurrency would introduce: Django's test client per thread needs its own DB connection and `APIClient` is not documented as safe for genuinely concurrent multi-threaded use, whereas the psycopg harness already solves exactly this (RFC v1.0 §17 in spirit: one proven mechanism, reused, not a second one built per test) | `tests/concurrency/test_hold_02.py` |
| Unlike CONC-01/02/05, HOLD-02 asserts `len(successes) == 0` UNCONDITIONALLY on every one of 50 runs, with no "retry the round on zero successes" logic | CONC-01's retry logic exists because zero successes is a documented LIVENESS characteristic of contention at scale (Key Technical Decisions, Phase 4/6) — a legitimate outcome to route around, not a bug. HOLD-02's zero is a SAFETY invariant instead: the range was never actually free (a hold already occupies it), so zero successes is the only correct result every single time, and retrying on "the test correctly detected zero bookings" would be nonsensical | `tests/concurrency/test_hold_02.py` |
| HOLD-01's acceptance step (RFC v1.0 §10.3 / Spec v1.0 §4.3) is proven by executing the literal conditional `UPDATE ... WHERE status='held' AND user_id=$2 AND expires_at > now()` directly in the test, not through an HTTP call | Explicit instruction for this phase: the offer-confirmation endpoint doesn't exist until Phase 16, and this follows the identical "mechanism proven directly, before its real caller" pattern already used for `actor_type='system'` (Phase 13) and containment eligibility (Phase 14). Two companion tests prove the predicate's two guard clauses matter, not just the happy path: an expired hold's acceptance affects 0 rows (the `expires_at > now()` clause), and a wrong `user_id` affects 0 rows too (Spec's `user_id = :user_id` clause — RFC v1.0 §10.3's own SQL snippet names this column `held_for_principal`, which doesn't exist in Spec v1.0 §3's actual DDL; `booking.user_id` already serves this role, per `Booking`'s own model docstring since Phase 2/3 — not a new decision, just the first phase to literally implement the acceptance SQL against it) | `tests/bookings/test_holds.py` |
| HOLD-03 and "GET /bookings never returns held rows" both already had Phase 6 coverage (`test_held_slot_never_reveals_identifying_fields_even_to_admin`, `tests/bookings/test_read_endpoints.py`) using an ORM-constructed held row — Phase 15 added COMPLEMENTARY tests using the real `create_booking(..., status=HELD)` mechanism, rather than treating the DoD boxes as already satisfied or duplicating coverage for its own sake | The Phase 6 tests proved the READ-side rule holds for *a* held row; Phase 15's tests prove it holds for a held row created through the mechanism this phase actually built — a meaningfully different (stronger, not redundant) claim, and the honest way to check off a DoD item that already happened to be true before this phase started | `tests/bookings/test_holds.py` (`test_hold_03_opaque_in_availability_view`, `test_holds_never_returned_by_get_bookings`) |
| The RECON-05 predicate-covers-'held' test (`tests/test_schema_assertion.py`) was written in Phase 3, not this phase | Confirmed by reading it, not assumed: Phase 2 already put `'held'` in the constraint predicate and Phase 3's schema-assertion work already asserted `pg_get_constraintdef` contains both `'held'` and `'confirmed'`. This phase's own DoD lists it as a checkbox, but the honest status is "verified still passing, docstring's stale 'held rows don't exist until Phase 15' phrasing corrected," not "written this phase" | `tests/test_schema_assertion.py` |
| `RecordableConflictError` (new base class) — `SlotUnavailableError`/`AlreadyOnWaitlistError`/`OfferExpiredError`/`OfferAlreadyResolvedError` all now subclass it and carry `code`/`message`/`http_status` as class attributes; `kairos_exception_handler` and `run_idempotent_write` each collapsed four isinstance branches into one | Phase 16 introduced the THIRD and FOURTH instance of the identical "409/422 outcome that must also be idempotently recordable" shape (after Phase 4's/Phase 14's). This project's own established rule — "worth extracting once real duplication exists, not before" (`KairosAPIView`, Phase 6) — was satisfied by the count reaching four, not by aesthetics; `ServiceUnavailableError` deliberately stays OUTSIDE this hierarchy since it carries `retry_after_seconds`, genuinely different response shape (a header, not just body fields) | `kairos/core/exceptions.py`, `kairos/core/drf.py`, `kairos/core/idempotency.py` |
| `WaitlistOffer.hold_booking` is a `OneToOneField`, not a plain `ForeignKey` | Spec v1.0 §3 declares `hold_booking_id UUID NOT NULL UNIQUE` — PRD FR23 (an offer without a hold is not permitted) and FR25 (at most one offer per freed range, enforced structurally via `no_overlapping_bookings` forbidding a second overlapping hold) both depend on this uniqueness being real, not incidental | `kairos/waitlist/models.py` |
| `create_offer_for_freed_range` (the cascade worker) is a NEW function in `kairos.waitlist.services`, reusing `create_booking(status=HELD)` for the hold-creation step rather than writing a bespoke insert | Same "one write path, one correctness proof" reasoning already applied twice (Phase 13's `actor_type`, Phase 15's `status`) — the hold this worker creates needs the IDENTICAL session-settings/SQLSTATE-translation/`refresh_from_db` machinery any other hold does, and reusing the function is what makes `SlotUnavailableError` (23P01, "something already occupies the range") already arrive translated and ready to catch-and-retry-next-candidate, rather than needing its own SQLSTATE handling | `kairos/waitlist/services.py` (`create_offer_for_freed_range`) |
| The Celery task wrapper (`kairos/waitlist/tasks.py`) imports `kairos.waitlist.services` INSIDE the task function body, not at module level; `kairos.bookings.services` imports the TASK module at module level | `kairos.waitlist.services` imports `kairos.bookings.services` (for `create_booking`); `kairos.bookings.services` needs to dispatch the cascade task after a cancellation, which would require importing `kairos.waitlist.tasks` — and if THAT module imported `kairos.waitlist.services` at its own module level, the two apps would form a genuine circular import (bookings.services → waitlist.tasks → waitlist.services → bookings.services). Deferring the ONE backward-pointing import to call time (by which point both modules are already loaded, regardless of which side started the chain) breaks the cycle without restructuring either app — verified via `manage.py check` actually succeeding, not just by reasoning about it | `kairos/waitlist/tasks.py`, `kairos/bookings/services.py` |
| `CELERY_TASK_ALWAYS_EAGER = True` in `kairos.settings.test`, combined with `django.test.TestCase.captureOnCommitCallbacks(execute=True)` in tests that need cancellation/decline to actually trigger cascade | The test suite must never depend on Redis being reachable (RFC v1.0 §4.3: Celery/Redis is a liveness dependency, not a correctness one — CI's `test` job doesn't run Redis, matching the `concurrency` job's own Postgres-only-via-plain-`docker-run` precedent). Eager mode makes `.delay()` run synchronously in-process; `captureOnCommitCallbacks` is Django's own officially-documented way to fire `on_commit()` hooks under the default ROLLBACK-based `db` fixture, without needing `transaction=True`'s real (slower) commits — WL-01/WL-02 (genuine multi-threaded races) still need `transaction=True` for real reasons and don't use this helper; the non-concurrent confirm/decline/cascade tests do | `kairos/settings/test.py`, `tests/waitlist/test_offers.py` |
| ⚠️ **Real bug caught building WL-02's test, then found to already exist in `decline_offer`**: releasing a hold (transitioning `booking.status` from `'held'` to `'cancelled'`) must also set `expires_at = NULL` | The `hold_has_expiry` DB CHECK constraint (Phase 2) requires `expires_at IS NULL` for any non-`'held'` row — an UPDATE that changes status without clearing `expires_at` fails outright with SQLSTATE 23514 (`check_violation`). Caught empirically (the constraint literally rejected WL-02's first draft), then traced to the SAME missing clause already sitting in `decline_offer`'s hold-release UPDATE, written earlier in this same phase — fixed in both places, not just the one that failed loudly first | `kairos/waitlist/services.py` (`decline_offer`), `tests/concurrency/test_wl_02.py` |
| WL-02's "reaper's expiry path" is simulated as `UPDATE booking SET status='cancelled', expires_at=NULL, cancelled_at=now() WHERE id=$1 AND status='held' AND expires_at<=now()` — an UPDATE to `'cancelled'`, never a DELETE and never an invented `'expired'` booking status | Derived from the schema, not guessed: `booking.status`'s CHECK constraint allows only `('confirmed','held','cancelled')` — there is no `'expired'` value at the booking level (that belongs to `waitlist_offer.status` only). RFC v1.0 §10.4's cleanup-on-write mechanism DOES use a DELETE, but that's a different, narrower mechanism (a resource+range-scoped sweep ahead of an INSERT); the reaper needs the row to remain addressable afterward to drive cascade (Phase 17), so UPDATE-to-`'cancelled'` — mirroring `decline_offer`'s own hold-release shape — is the form Phase 17's real reaper will need regardless of who builds it | `tests/concurrency/test_wl_02.py` |
| WL-02's 100 repetitions are split 50/50 between a hold whose `expires_at` is safely in the future (acceptance must structurally win) and safely in the past (the reaper must structurally win), rather than setting `expires_at` right at the barrier-release boundary and hoping timing jitter produces a natural mix | For a FIXED `expires_at`, exactly one of `expires_at<=now()` / `expires_at>now()` can be true — which one wins is therefore determined by wall-clock time vs. that fixed value, not by which of two symmetric competitors' transaction commits first (unlike CONC-01's genuine coin-flip). Deterministically constructing both orderings is a more reliable, less flaky proof that "exactly one wins, never both, never neither" holds in EITHER direction than gambling on microsecond-scale scheduling variance to exercise both cases across a CI run | `tests/concurrency/test_wl_02.py` |
| `ClientOutcome` (the shared concurrency harness) gained an additive `rowcount: int | None = None` field | A conditional `UPDATE`/`DELETE` with a WHERE clause matching zero rows is NOT a Postgres error — the pre-existing `success`/`sqlstate` fields can't distinguish "my UPDATE won the race" from "it silently matched nothing," which is the entire question WL-02 asks of both competing statements. Backward-compatible (defaults to `None`, every existing CONC/HOLD-02 test is unaffected) rather than a parallel, WL-02-specific harness | `tests/concurrency/harness.py` |
| `decline_offer` sets the declining `waitlist_entry`'s status to `EXPIRED`, not back to `WAITING` | `find_eligible_entries` orders by `joined_at` — a declining entry that returned to `'waiting'` would RETAIN its (earliest) `joined_at` and immediately out-compete every other candidate for the very cascade its own decline just triggered, for the identical range it just turned down. Routing it to `EXPIRED` instead (a status value present in the schema since Phase 14 but never actually reachable until now) also finally gives that enum value real meaning: "this entry's specific opportunity is over," distinct from `CANCELLED` (the user withdrew the whole request, Phase 14) and `FULFILLED` (accepted) | `kairos/waitlist/services.py` (`decline_offer`) |
| Decline (`OfferAlreadyResolvedError`, 409 `offer_already_resolved`) is deliberately NOT idempotent-as-a-200-no-op the way `cancel_booking`/`cancel_waitlist_entry` are | Every other cancel-shaped endpoint in this codebase treats "already in the target state" as success (Spec v1.0 §5.6's explicit convention, applied consistently since). Decline is the one exception BECAUSE Spec v1.0 §5.13 names `offer_already_resolved` as its own distinct code — a deliberate Spec choice honored here rather than silently generalized to match the more common pattern | `kairos/core/exceptions.py`, `kairos/waitlist/services.py` (`decline_offer`) |
| `POST /waitlist-offers/{id}/confirm`'s response is the BOOKING with `waitlist_offer_id` added at the response-serialization layer, not a new field on `BookingResponseSerializer` itself | Spec v1.0 §3's `booking` DDL has no `waitlist_offer_id` column — Spec v1.0 §5.13's "with `waitlist_offer_id` populated" describes the RESPONSE shape, resolved via the reverse `hold_booking` relation, not a stored field. Adding it to the shared serializer would put a field on EVERY booking response (create, GET, edit, cancel) that only this one endpoint's Spec entry actually promises, and would break every existing exact-`set(body.keys())` test elsewhere in this codebase for no reason | `kairos/waitlist/views.py` (`WaitlistOfferConfirmView`) |
| `POST /waitlist-offers/{id}/decline` requires `Idempotency-Key` despite Spec v1.0 §7's coverage list not naming it (mirroring `/confirm`, which IS named) | The third application of this project's own established broader-than-Spec precedent (`POST /recurring-series/{id}/cancel`, Phase 12; `POST /waitlist-entries/{id}/cancel`, Phase 14) — consistency, plus Spec v1.0 §7 point 7's GENERAL "conflict outcomes are recorded too" rule applies to `offer_already_resolved` exactly as it does to every other `RecordableConflictError` | `kairos/waitlist/views.py` (`WaitlistOfferDeclineView`) |
| WL-01's B1/B2 setup uses ADJACENT (non-overlapping) confirmed bookings, not literally the overlapping example times Test Plan v1.0's prose gives | Two genuinely overlapping ranges cannot both be `status='confirmed'` on one resource simultaneously — that's the exact guarantee this whole project exists to enforce, so Test Plan's literal "B1 (09:00–10:00) and B2 (09:30–10:30) both confirmed" cannot describe two simultaneously-live rows as written. Read "two OVERLAPPING SIMULTANEOUS cancellations" as the cancellations executing concurrently in TIME (barrier-released), not the bookings' ranges overlapping each other — consistent with the test's own real purpose (proving the cascade code path is safe under genuine concurrent execution, the same rigor already given the bare constraint by CONC-01/HOLD-02) | `tests/concurrency/test_wl_01.py` |
| WL-01 calls `cancel_booking` (the real Python service function, via real OS threads under `transaction=True`) rather than raw psycopg SQL | Unlike CONC-01–05/HOLD-02 (which deliberately prove the bare constraint, independent of application code), WL-01's actual subject IS the application-level cascade path — `create_booking(status=HELD)`'s retry-on-conflict loop and the `on_commit`-dispatched worker — so testing it via raw SQL would prove nothing about the code this phase actually wrote. Django provides a genuine per-thread connection automatically, and `CELERY_TASK_ALWAYS_EAGER` makes the dispatched cascade run synchronously within whichever thread's `on_commit` fires it | `tests/concurrency/test_wl_01.py` |
| Cleanup-on-write's DELETE runs INSIDE `create_booking`, unconditionally for every caller — not as a separate opt-in step, not skipped for the cascade worker's own hold-creation calls | Its scope (`resource_id` + `time_range &&`) makes it correct regardless of what kind of row is about to be written: if an expired hold sits in the way, whoever writes next clears it, whether that writer is an ordinary user, a background job, or the cascade worker creating a DIFFERENT hold nearby. Making it conditional on the caller would need a reason to exclude some caller from PRD FR18 ("an expired hold must not block ANY booking") — none exists | `kairos/bookings/services.py` (`create_booking`) |
| The cleanup DELETE uses `tstzrange(%s, %s)` (two bound datetime params) rather than building a range literal string for the raw SQL | Avoids manual string interpolation of a range boundary entirely — `tstzrange()` is a genuine SQL function taking two ordinary parameterized values, safer and simpler than the `range_literal()` string-building helper `tests/concurrency/harness.py` needs (that helper exists because raw psycopg test code has no Django ORM to lean on; application code does) | `kairos/bookings/services.py` (`create_booking`) |
| `reap_expired_holds` reclaims each expired hold in its OWN independent transaction, not one transaction for the whole sweep | Mirrors `confirm_recurring_series`/`rolling_materialize_series`'s established per-item isolation (Phase 12/13): one hold losing its race to a concurrent acceptance (RECLAIM-03) must never roll back the reclamation of every OTHER expired hold the same sweep found — an all-or-nothing transaction would turn one contested row into a liveness failure for unrelated ones | `kairos/waitlist/services.py` (`reap_expired_holds`) |
| The reaper's reclaim UPDATE is `WHERE id=... AND status='held' AND expires_at<=now()` — a CONDITIONAL update guarded on current status, identical in shape to `accept_offer`'s own guard, never a blind/unconditional one | This IS RECLAIM-03's race-safety claim made concrete: whichever of the reaper's UPDATE or acceptance's UPDATE commits first wins the row outright (Postgres serializes access to it); the loser's WHERE clause simply matches zero rows on re-evaluation. RFC v1.0 §10.4's own words — "the race... is safe in both orderings" — describe exactly this mechanism, not a coincidence of timing | `kairos/waitlist/services.py` (`reap_expired_holds`) |
| `dispatch_cascade` (new, `kairos/waitlist/tasks.py`) is the ONE place `.delay()` is ever called from — `cancel_booking` and `decline_offer` both go through it, neither calls `create_offer_for_freed_range_task.delay(...)` directly any more | A `transaction.on_commit()` callback runs synchronously right after a REAL, already-successful commit — an exception escaping it propagates into the original request/caller, turning an already-committed cancel/decline into an apparent failure. WL-06 requires exactly the opposite ("booking creation and cancellation succeed... degraded liveness, the safe direction"). One wrapper, reused by both callers, rather than duplicating the `try/except` — verified LIVE, not just reasoned about (see the Phase 17 Completed Phases row) | `kairos/waitlist/tasks.py` (`dispatch_cascade`) |
| `dispatch_cascade` catches a broad `Exception`, not a specific `kombu`/`redis` exception class | The LIVE WL-06 verification's actual traceback bottoms out in `kombu.exceptions.OperationalError` wrapping `redis.exceptions.ConnectionError` — but enumerating exact broker-library exception classes risks missing one and silently reintroducing the exact crash this wrapper exists to prevent. The cost of catching broadly here is low (this call site's only job is a fire-and-forget dispatch with no other side effect to mask) and the cost of catching too narrowly is a repeat of the WL-06 failure mode itself | `kairos/waitlist/tasks.py` (`dispatch_cascade`) |
| `hold_reaper_heartbeat_is_stale` (new) reads `system_check_run` directly and returns a bool — it does NOT alert, page, or expose an endpoint | Implementation Plan Phase 17's own Scope IN is explicit: "Heartbeat alerting → Phase 21 (the heartbeat is written here)." WL-05 Part B's literal text names `GET /api/v1/admin/checks/latest` as the surface an alert would surface through — that endpoint doesn't exist yet (no phase before 21 builds it). Testing the DATA (is staleness genuinely detectable from what Phase 17 writes) rather than inventing an endpoint or alert pipeline out of scope is the honest middle path — the same discipline this project applied to Phase 13's "records a notification is due, sends nothing" gap | `kairos/waitlist/services.py` (`hold_reaper_heartbeat_is_stale`), `tests/waitlist/test_reclamation.py` |
| RECLAIM-04 was ACTUALLY RUN at full DoD scale (200 writers × 50 runs) before this phase's own Definition of Done was marked satisfied — not estimated, not left at a reduced scale | Explicit instruction: "State which outcome actually occurred, with real numbers, before claiming this Definition of Done item is satisfied." Real result: 269 SQLSTATE 40P01 deadlocks across the 50 runs (roughly half the runs saw at least one; the other half saw zero) — NOT the DoD's literal "zero deadlocks." Per the SAME instruction's own anticipated second outcome: this is a documented liveness finding, not a failure — safety (never more than one success per round) held on all 10,000 attempts, every failure SQLSTATE was already in the documented retryable set, and 40P01 was ALREADY mapped to a retryable 503 by `BookingService` since Phase 4, before this phase ever touched it | `tests/concurrency/test_reclaim_04.py` |
| RECLAIM-04 is excluded from the `concurrency` CI job (`--ignore`d in `.github/workflows/ci.yml`), though the test file exists and was run manually | Test Plan v1.0 §13 places RECLAIM-04 in the STAGING/pre-release tier explicitly (RECLAIM-01–03 are CI tier and run in the job normally) — 200×50 = 10,000 raw attempts taking over 6 minutes is expensive enough that per-commit execution would make CI unusable, Test Plan's own stated reason for the tier's existence, and the identical treatment CONC-01's own full 100-run+N=500 escalation already has (deferred to Phase 28, run manually/scheduled) | `.github/workflows/ci.yml`, `tests/concurrency/test_reclaim_04.py` |
| WL-05 Part A and WL-06 were verified by ACTUALLY stopping the real `beat`/`redis` Docker containers and driving a real `manage.py runserver` (`kairos.settings.dev`, genuinely non-eager Celery) over real HTTP — not simulated, not mocked, no pytest test claims to cover them | Explicit instruction: a mocked simulation would not prove RFC v1.0 §4.3's real degradation behavior under a real outage; `CELERY_TASK_ALWAYS_EAGER` (test settings) means `.delay()` never touches a broker under pytest AT ALL regardless of Redis's real state, so no pytest test could have proven this even if it tried. `test_dispatch_cascade.py` is explicit about being a regression guard for the try/except's continued EXISTENCE, not a substitute for the live proof | manual verification, this session (see the Phase 17 Completed Phases row for the transcript); `tests/waitlist/test_dispatch_cascade.py` |
| `kairos_dev` required a fresh `manage.py migrate` (superuser DSN) before the WL-05/WL-06 live verification — Phase 14/15/16/17's migrations had never been applied to it | `kairos_dev` is a separate, long-lived database from the ephemeral `kairos_test` pytest creates and destroys per run; nothing in the ordinary `pytest` workflow ever touches it. Routine to catch (the exact "Migrations need DDL privileges kairos_app doesn't have" step README already documents), not a new gap — flagged here only because it was a genuine precondition for this phase's live verification specifically | `README.md` §"Running Locally" |
| `NotificationService` (Phase 18) wraps Django's own `EMAIL_BACKEND` abstraction (console/smtp/locmem) rather than a bespoke pluggable-backend hierarchy | `EMAIL_BACKEND` already IS the "console for dev, SMTP for prod, capturing for tests" seam this phase asks for — reusing it means a delivery failure raises the exact exception class Django's backend raises, and `locmem`'s `django.core.mail.outbox` IS the capturing backend, for free. The same "reuse a framework/database guarantee instead of reinventing it" choice already made for `CompositePrimaryKey` (Phase 5), `set_config` over literal `SET` (Phase 4), and Postgres triggers over an application-level audit call (Phase 8) | `kairos/core/notifications.py`, `kairos/settings/{base,dev,test,prod}.py` |
| Every `notify_*` function only builds a message and calls `dispatch_notification` (enqueue-and-return) — the actual `send_mail()` call happens inside `send_notification_task`, in a worker, never inline | RFC v1.0 §15a: notification dispatch must be asynchronous, "called from workers, never synchronously from the request path." This holds even for notify_* calls that originate FROM a worker already (`notify_offer_created` inside `create_offer_for_freed_range`, itself already running inside a Celery task) — decoupling "decide to notify" from "attempt delivery" means a slow/failing SMTP call never blocks the offer-creation worker's own completion either, not just the HTTP request path | `kairos/core/notifications.py`, `kairos/core/tasks.py` (`dispatch_notification`, `send_notification_task`) |
| `dispatch_notification` (Phase 18) mirrors `kairos.waitlist.tasks.dispatch_cascade` (Phase 17) exactly — same broad `except Exception`, same "log and swallow" shape, same reasoning | `cancel_booking`'s admin-cancellation notification is registered via the identical `transaction.on_commit()` pattern the cascade dispatch already uses — an exception escaping `dispatch_notification` would propagate into that already-successful commit's caller for the SAME reason WL-06 already proved matters for cascade. One proven shape, reused, not a second one invented for a structurally identical problem | `kairos/core/tasks.py` (`dispatch_notification`) |
| `NotificationLog` (Phase 18) is new schema — Spec v1.0 §3 has no notification concept at all | The identical situation Phase 9's `user_group`/`user_group_membership` tables were in: PRD FR55 ("must be recorded and retried") concretely requires somewhere to record attempts/status, and no such table exists in the six source documents. Minimal shape only: one row per logical notification, `attempts`/`status`/`last_error` — no delivery-provider-specific fields, no template versioning, nothing beyond what FR55 literally asks for | `kairos/core/models.py` (`NotificationLog`) |
| `NotificationLog` gets NO audit trigger, unlike `waitlist_entry`/`waitlist_offer` | It isn't one of the five entities Spec v1.0 §3/RFC v1.0 §12 name as audited business state — it's a delivery-outcome LOG, the same category `system_check_run` (Phase 13) already established with no trigger of its own. `kairos_app` is granted UPDATE (unlike `audit_log`/`system_check_run`'s append-only SELECT/INSERT) because a retry genuinely revises the SAME row's `status`/`attempts` in place, matching `waitlist_entry`/`waitlist_offer`'s grant shape instead | `kairos/core/migrations/0011_kairos_app_notification_log_grants.py` |
| The delivery-attempt logic lives in a plain function, `_execute_notification_delivery`, not inlined into the `@shared_task` body | Lets a test drive a failure-then-success sequence for the SAME notification id directly, proving the `attempts`-accumulates/`status`-reflects-latest-outcome mechanism, without depending on Celery's own retry scheduling or `CELERY_TASK_ALWAYS_EAGER`'s timing behavior under retry_backoff — the identical "test the mechanism directly" precedent `expand_occurrences` (Phase 11) and `reap_expired_holds` (Phase 17) already established for logic awkward to exercise through its real transport | `kairos/core/notifications.py` (`_execute_notification_delivery`), `tests/test_notifications.py` |
| `notify_rematerialization_conflict` (Phase 18) was built even though Phase 18's own Scope IN names only ONE tzdata re-materialization notification point | Not new scope: `rematerialize_stale_series` (Phase 13) has recorded `"resource_administrators": "pending Phase 18 delivery"` in its own conflict findings since it was written — PRD FR13b requires a conflict be surfaced for human decision, and leaving that specific, already-committed placeholder unfulfilled after building the entire notification infrastructure this phase exists to build would be a loose end this project's own discipline (see the Phase 12→13 REC-06/horizon precedent) argues against leaving | `kairos/bookings/tasks.py` (`rematerialize_stale_series`), `kairos/core/notifications.py` (`notify_rematerialization_conflict`) |
| `notify_rollback_hold_released` (Phase 18) was built standalone, with NO real trigger wired to it, per this phase's own explicit clarification | Rollout v1.0 §4.5's hold release is a manual operational runbook (SQL an operator runs during an incident) — no phase in the Implementation Plan has built application code that performs it. Inventing a fake "rollback release" code path just to give this notification a caller would be scope beyond this phase's actual job (this phase's job is notification DELIVERY, not automating an incident-response runbook); the template/message content and the `NotificationService.send` mechanism exist and are independently tested with a manually-constructed event instead, the same "mechanism proven ahead of its real caller" pattern already used repeatedly (`actor_type='system'`, containment eligibility, hold acceptance) | `kairos/core/notifications.py` (`notify_rollback_hold_released`); see Open Questions |
| ⚠️ **Real bug caught only by rebuilding the worker's Docker image and re-reading its startup banner**: `send_notification_task` was silently ABSENT from the running worker's registered task list after `docker compose restart worker` — even though it lives in `kairos/core/tasks.py`, the exact file `autodiscover_tasks()` scans (Phase 13's own hard-learned convention, followed correctly this time) | `docker compose restart` restarts a container from its EXISTING image; it does not rebuild from changed source. The worker came back up cleanly with zero errors anywhere and simply didn't list the new task — the identical "no errors, just absence" failure mode RFC v1.0 §14 warns about, this time one layer down the stack from Phase 13's cause (a wrong file, then; a stale image, now). Fixed with `docker compose up -d --build worker beat`; re-verified by reading the rebuilt banner (six tasks) and dispatching two real tasks against it — one deliberately malformed, which demonstrated genuine exponential retry-with-backoff (1s/0s/4s/2s/6s, jittered) against the real Redis broker before failing cleanly at `NOTIFICATION_MAX_RETRIES`; one valid, which printed to the worker's console `EMAIL_BACKEND` and left one `notification_log` row (`status='sent'`), confirmed via `psql` | this session's live verification (see the Phase 18 Completed Phases row) |

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

Background jobs (Phase 13) — `docker compose up -d` (the same command above) now ALSO starts
`redis`, `worker`, and `beat`; nothing extra to run. Verified live: both containers connect
to Redis and Postgres (as `kairos_app`) successfully, the worker's own startup log lists all
registered tasks (six as of Phase 18 —
`rolling_materialize_series_task`/`rematerialize_stale_series_task`/`check_tzdata_drift_task`/
`create_offer_for_freed_range_task`/`reap_expired_holds_task`/`send_notification_task`), and a
manually dispatched task of each kind completed successfully against the real running worker.
`manage.py rematerialize_series` runs both materialization jobs synchronously, once, without
a worker at all — the "on deploy" trigger RFC v1.0 §9.4 asks for, sharing the exact same
functions Celery Beat calls on schedule (hourly by default —
`ROLLING_MATERIALIZATION_INTERVAL_SECONDS`/`TZDATA_REMATERIALIZATION_INTERVAL_SECONDS`; the
drift check is daily, `TZDATA_DRIFT_CHECK_INTERVAL_SECONDS`; the hold reaper is every 30s,
`HOLD_REAPER_INTERVAL_SECONDS`, Phase 17 — all tunable via `.env.example`).

Reclamation (Phase 17) — verified live against the real Docker stack, with `beat`/`redis`
genuinely stopped (`docker compose stop beat` / `stop redis`), not simulated: with `beat`
down, a hold seeded with a 3-second expiry sat `status='held'` 12+ seconds past `expires_at`
with zero errors anywhere; with `redis` down, `POST /bookings`, `POST /bookings/{id}/cancel`,
and a booking over an already-expired hold (cleanup-on-write) all still succeeded over real
HTTP against `manage.py runserver` (`kairos.settings.dev`), the cancellation's cascade
dispatch failed with a genuine `kombu.exceptions.OperationalError` caught and logged
(`cascade_dispatch_failed_broker_unavailable`) rather than surfacing as a request failure, zero
`waitlist_offer` rows were created, and the worker reconnected cleanly once `redis` restarted.
`kairos_dev` needed a fresh `manage.py migrate` first — Phase 14 through 17's migrations had
never been applied to it before this session (only `kairos_test`, which `pytest` creates and
destroys fresh every run, had been current).

Notifications (Phase 18) — verified live against the real Docker stack, not just pytest's
eager-mode simulation. ⚠️ **The exact "silently absent from the registered task list"
failure mode Phase 13 already caught once (RFC v1.0 §14's "no errors, just absence") recurred
here for a different reason**: `send_notification_task` was correctly placed in
`kairos/core/tasks.py` (the file `autodiscover_tasks()` actually scans), but `docker compose
restart worker` only restarts the EXISTING container from its already-built image — it does
not rebuild from the changed source. The worker came back up cleanly, logged no error, and
simply didn't list the new task, until `docker compose up -d --build worker beat` rebuilt the
image and the task appeared in the startup banner (six tasks total). Caught by reading the
banner, not by any automated check — the identical verification discipline Phase 13's own
entry already established. After the rebuild, two tasks were dispatched manually via
`send_notification_task.delay(...)` against the real running worker (`kairos.settings.dev`,
genuinely non-eager Celery, real Redis broker): the first (deliberately malformed
`recipient_user_id`) demonstrated REAL exponential retry-with-backoff live — five retries at
1s/0s/4s/2s/6s (jittered, growing), then a clean final failure once `NOTIFICATION_MAX_RETRIES`
was exhausted, with zero `notification_log` row ever created (the ValidationError fired before
the INSERT could execute) — genuine proof of `retry_backoff=True` scheduling real delays
through a real broker, which `CELERY_TASK_ALWAYS_EAGER` structurally cannot exercise under
pytest. The second (valid) dispatch printed the full email (subject, body, headers) to the
worker's own stdout via the console `EMAIL_BACKEND`, logged `notification_delivered`, and left
exactly one `notification_log` row with `status='sent'`, `attempts=1` — confirmed directly via
`psql` against `kairos_dev`, not just the log line.

The frontend starts Phase 23.

## Running Tests

```bash
cd backend
pytest tests/concurrency -v   # Milestone 1 — the project's central proof, run this first
pytest                        # full suite: the above + smoke test + schema assertion
```

`tests/concurrency/` — CONC-01 (N=200 identical-slot, 10 runs), CONC-02 (partial + 5-way
chained overlap, 10 runs each), CONC-03 (edit-vs-create race, Phase 7, 10 runs), CONC-04
(edit-vs-edit race, Phase 7, 10 runs), CONC-05 (cancel-and-rebook race, 10 runs), HOLD-02
(Phase 15: 50 barrier-released `booking` INSERTs against an actively held range, 50 runs —
asserts ZERO successes unconditionally every run, not "at most one," since the range was
never actually free), WL-01 (Phase 16: two real threads calling `cancel_booking` itself, not
raw SQL — the cascade code path is this test's actual subject — 50 runs, ground truth via
`count_overlapping_pairs`), WL-02 (Phase 16: 100 runs, simulated-reaper-expiry vs. real
acceptance racing on one hold row, split 50/50 between orderings), RECLAIM-03 (Phase 17: 100
runs, cleanup-on-write's DELETE+INSERT vs. the real acceptance UPDATE). RECLAIM-04 (Phase 17:
200×50, deadlock-under-load) is a fifth file in this directory but is EXCLUDED from this
default sweep — see below. Each CONC round is retried up to 10 times only if it produced zero
successes (a documented, load-correlated liveness characteristic — see Key Technical
Decisions); more than one success on any single attempt fails immediately and is never
retried — HOLD-02/WL-01/WL-02/RECLAIM-03 have no such retry logic, since their outcome is a
SAFETY invariant (zero successes; exactly one race winner) checked unconditionally on every
single attempt, not a liveness characteristic to route around. CONC-03/04/HOLD-02/WL-02/
RECLAIM-03/RECLAIM-04 exercise raw UPDATE/INSERT/DELETE SQL directly through the same
barrier-released harness as the others, not through the service/view layer — proving the
constraint itself, independent of application code (the HTTP-level translation HOLD-02 does
NOT re-prove is instead covered by `tests/bookings/test_holds.py`'s real-HTTP step; WL-01
deliberately breaks this pattern — see Key Technical Decisions for why).
`tests/test_schema_assertion.py` (RECON-05 CI form) fails the moment
`no_overlapping_bookings`'s predicate is narrowed — verified by hand during Phase 3 (narrowed
it, watched the test fail, reverted). `tests/test_audit_trail.py` (Phase 8) — AUD-01
(connecting AS `kairos_app` via a dedicated psycopg connection, not the test session's own
DB role, and watching `UPDATE`/`DELETE` on `audit_log` fail with `InsufficientPrivilege`; a
direct grant-catalog inspection too, so a future migration can't silently widen them
unnoticed), AUD-02 (a raw SQL write to `booking` still produces an audit row; all four
audited-table triggers exist — the original three from Phase 8 plus `waitlist_entry` since
Phase 14, the test renamed and its assertion widened rather than left stale), and the
`actor_type='system'` attribution mechanism (no real worker
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

`tests/bookings/test_recurrence.py` (Phase 11) — unit tests against `expand_occurrences`
directly (no HTTP layer exists yet): TZ-01 (America/New_York, all four occurrences render
10:00 local, Nov 1 resolves EST not EDT since the transition happens before the 10:00
occurrence that day), TZ-05 (Paris nonexistent 02:30→03:30, both start and end shifted by
the same computed gap), TZ-06 (Paris ambiguous 02:30, first/pre-transition instance, no
shift), TZ-09 (Sydney's October AND April transitions — opposite hemisphere, catches a sign
error every northern-hemisphere test would miss), TZ-10 (Kolkata, zero DST, identical offset
across six occurrences spanning dates where Paris/New York both transition);
`occurrence_count=101` raising `PolicyValidationError` and `=100` succeeding; plus
`recurring_series` schema tests (round-trips `series_start_date`/`tzdata_version`, rejects a
fixed-offset timezone via the same `validate_iana_zone` path as `Resource`, and the DB
`CHECK` constraint backstop firing on a bulk write that bypasses `expand_occurrences`
entirely) and a `Booking.series` FK round-trip through `BookingResponseSerializer`.

`tests/bookings/test_recurring_series.py` (Phase 12) — REC-01 (preview's zero-booking-row
ground truth), REC-02 (unacknowledged conflicts → 409, zero bookings), REC-03 (a conflict
arising between preview and confirm reports `acknowledged: false`, distinguishable from a
known one), REC-04 (expired token → 409 `preview_expired`, via `monkeypatch`ing
`PREVIEW_TOKEN_TTL_SECONDS` to a negative value rather than sleeping 15 real minutes), REC-05
(occurrence 6 survives occurrence 7's real, induced conflict — the actual, airtight proof of
independent transactions: any statement error aborts Postgres's WHOLE transaction absent a
savepoint, so occurrence 6 surviving occurrence 7's failure is only possible if they were
never in the same transaction to begin with), a dedicated test confirming one correctly-
attributed `audit_log` row per created occurrence (Phase 12's own explicit instruction — see
Key Technical Decisions for a correction: this test's `occurred_at` values are NOT, by
themselves, evidence of independent transactions — Django's `Now()` compiles to Postgres's
`statement_timestamp()`, which varies per statement regardless of transaction boundaries),
REC-06's full
bound matrix (0/100/101 occurrences, within/beyond the 365-day horizon), REC-07 (a preview→
confirm round trip through TZ-01's exact DST-spanning series produces byte-identical
instants), and IDEM-05 (a replayed confirm returns the IDENTICAL `created`/`conflicts`
arrays, proven by asserting no additional bookings exist after the replay). Also covers a
token issued to a different user (rejected identically to expired — no information leak
about which), a fixed-offset timezone rejected at preview, and recurring-series cancel
(future-only, owner-or-admin, 404 for a stranger). The full preview→confirm→cancel flow was
ALSO verified live over real HTTP against `manage.py runserver` connected as `kairos_app`
(the least-privilege role, not the superuser) with ground truth read back via `psql`, not
only through the Django test client.

`tests/bookings/test_rematerialization.py` (Phase 13) — both jobs called directly, no worker
needed: rolling materialization extending a partially-materialized series and respecting the
365-day horizon (including a no-op case for an already-fully-materialized series), TZ-07
(a stale occurrence recomputed from the series definition, local wall-clock preserved, stored
instant changed, `tzdata_version` bumped, `system_check_run` recorded — and a companion test
confirming a series whose occurrences happen NOT to have changed still gets marked current),
TZ-08 (a real, induced conflict — another user's actual booking occupying the recomputed
range — flags without dropping the occurrence, records both the series owner and resource
admin as needing notification, and lets the rest of the series still succeed; the conflicted
series deliberately keeps its OLD `tzdata_version` so a future run retries it), and two
spy-on-cursor tests (the exact style Phase 7/8/9 established) proving `app.actor_type='system'`
is genuinely visible at the point of the write, with the persisted `audit_log` row's
`actor_type`/`actor_id` checked too, not just the session variable. `tests/test_tzdata_check.py`
(Phase 13) — TZ-03 Test B: alerts (logs) without raising when behind, reports OK when current,
handles an unreachable endpoint gracefully; `fetch_latest_tzdata_version` itself was ALSO
verified live against the real PyPI endpoint outside the test suite (see Key Technical
Decisions), unlike the OIDC JWKS path it's modeled after.

`tests/waitlist/test_eligibility.py` (Phase 14) — WL-04 ★ against `find_eligible_entries`
directly: a freed 10:00–10:30 does NOT make a 10:00–11:00 waitlister eligible (the case that
would incorrectly pass under `&&` overlap semantics), a freed 10:00–11:00 does; excludes
non-`waiting` entries; FCFS ordering by `joined_at`/`id`; scoped per resource.
`tests/waitlist/test_views.py` (Phase 14) — join (201 shape, SEC-03(a) forged-`joined_at`
ignored, SEC-03(b)/(c) duplicate-live-entry and joining-while-offered both 409
`already_on_waitlist`, the 409 conflict outcome recorded and replayed per Spec v1.0 §7 point
7, 422 `slot_already_available` on a fully free range, 404 on a missing/inactive resource,
400 on a malformed range or a missing `Idempotency-Key`, an audit row on join), list
(self-scoped, `status` filter, `queue_position`), and cancel (owner-only 200, non-owner 404,
idempotent double-cancel, an audit row on cancel) — all against the real HTTP surface, not
just the service layer.

`tests/bookings/test_holds.py` (Phase 15) — HOLD-01 ★ end to end: a hold created directly via
`create_booking(..., status=HELD)`, an unrelated user's real `POST /bookings` for the exact
same range returns 409 `slot_unavailable`, and W's acceptance (the literal RFC v1.0 §10.3
conditional `UPDATE`, executed directly since no HTTP endpoint exists until Phase 16) flips
the same row — same `id`, `status` transitioned, `expires_at` NULL, no second row. Two
companion tests prove the acceptance predicate's other guard clauses matter: an expired
hold's acceptance affects 0 rows, a wrong `user_id`'s affects 0 rows too. HOLD-03 (opaque in
availability) and "`GET /bookings` never returns held rows," both proven again here using the
real Phase 15 hold-creation mechanism specifically, complementing (not replacing) Phase 6's
pre-existing coverage of the same rules against an ORM-constructed held row.
`tests/concurrency/test_hold_02.py` (Phase 15) — HOLD-02: 50 barrier-released raw-SQL
`booking` INSERTs against an actively held range, 50 runs, asserting ZERO successes
unconditionally on every run (a safety invariant, not a liveness one — no retry-on-zero logic
the way CONC-01 has) plus ground truth that the held row itself was never mutated.

`tests/waitlist/test_offers.py` (Phase 16) — cancellation-triggers-offer end to end (hold
created before the offer row, correct entry, correct linkage, an audit row on the hold with
`actor_type='system'`; no eligible entry creates nothing), confirm (converts the hold in
place — same `id`, no second booking row; 409 `offer_expired` on an expired hold; 404 for a
non-owner; a missing `Idempotency-Key` is 400; a replay returns the byte-identical response),
decline (releases the hold, cascades to the next entry, 409 `offer_already_resolved` on a
second decline, 404 for a non-owner), and WL-03 (cascade reaches entry 2 not 3 after entry 1's
offer is declined; skips a withdrawn entry 2 to reach entry 3) — all via
`django.test.TestCase.captureOnCommitCallbacks(execute=True)` so the REAL `on_commit()` →
`.delay()` → cascade chain runs, not just the underlying function called directly.
`tests/concurrency/test_wl_01.py` (Phase 16) — WL-01: two real OS threads calling
`cancel_booking` (not raw SQL — this test's subject IS the cascade code path) under
`transaction=True`, 50 runs, ground truth via `count_overlapping_pairs` (RFC v1.0 §14's own
reconciliation shape) plus confirming both cascades actually produced a hold.
`tests/concurrency/test_wl_02.py` (Phase 16) — WL-02: 100 barrier-released runs (50 with the
hold's `expires_at` in the future, 50 in the past — deterministically covering both orderings)
racing a simulated reaper-expiry `UPDATE` against the real RFC v1.0 §10.3 acceptance `UPDATE`
on the identical row; exactly one affects 1 row, the other 0, every single run.

`tests/bookings/test_cleanup_on_write.py` (Phase 17) — RECLAIM-01: a booking succeeds over a
seeded expired hold with no reaper anywhere in the test (the DELETE inside `create_booking` is
the only thing that could have cleared it), the hold row is genuinely GONE afterward (not
merely superseded); a hold that HASN'T expired yet survives an overlapping write attempt (which
itself then correctly 409s); an expired hold on a non-overlapping range is untouched by a write
elsewhere on the same resource (the DELETE's own `time_range &&` scoping).
`tests/waitlist/test_reclamation.py` (Phase 17) — RECLAIM-02: `reap_expired_holds` cascades to
the next eligible entry with zero booking traffic (called directly — Test Plan's own
"controllable time" requirement, not a real 30s wait); a hold with no eligible entry is
released without cascading; every run writes a `hold_reaper` heartbeat; a reclaimed hold's
audit row shows `actor_type='system'`. WL-05 Part B: no heartbeat at all is stale; a heartbeat
older than the threshold is stale; a fresh one isn't.
`tests/concurrency/test_reclaim_03.py` (Phase 17) — RECLAIM-03: 100 barrier-released runs,
cleanup-on-write's DELETE+INSERT (mirroring `create_booking`'s own statement order) racing the
literal RFC v1.0 §10.3 acceptance `UPDATE`; correctness inferred by correlating the two
outcomes (whichever wins, the other's failure mode is structurally determined), ground truth
exactly one active row every run.
`tests/concurrency/test_reclaim_04.py` (Phase 17) — RECLAIM-04, run manually at full DoD scale
(200 writers × 50 runs) with real, reported numbers (see the Phase 17 Completed Phases row) —
deliberately excluded from the CI `concurrency` job (Test Plan v1.0 §13's staging tier); run
before a release with `pytest tests/concurrency/test_reclaim_04.py -v -s`.
`tests/waitlist/test_dispatch_cascade.py` (Phase 17) — a regression guard proving
`dispatch_cascade` swallows and logs a broker failure rather than propagating it; WL-05 Part A
and WL-06 themselves were verified LIVE against a real Docker stack, not by any pytest test
(`CELERY_TASK_ALWAYS_EAGER` makes a genuine broker outage unreproducible under pytest) — see
the Phase 17 Completed Phases row for the full transcript.

`tests/test_notifications.py` (Phase 18) — content proofs for all four wired notification
points via the `locmem` capturing backend (`django.core.mail.outbox`): offer-created states
the expiry explicitly in the subject; admin-cancellation includes the recorded reason;
re-materialization states both the old and new instant; rollback-hold-released reads
distinctly from an ordinary offer-created notification (asserted by direct content
comparison, not just "contains a different string"). PRD FR55's "recorded and retried" is
proven by driving `_execute_notification_delivery` directly through a failure-then-success
sequence for the SAME notification id and asserting `NotificationLog.attempts` accumulates
while `status` reflects the latest outcome — bypassing Celery's own retry scheduling
entirely, the same reasoning `test_send_notification_task_is_configured_to_retry_with_backoff`
documents for why it only asserts the retry decorator's configuration rather than exercising
real backoff timing. `test_admin_cancellation_commits_even_if_notification_dispatch_fails`
is the direct DoD proof that a simulated provider outage doesn't fail the underlying
operation — patches `send_notification_task.delay` to raise and asserts the booking still
commits `CANCELLED`. `test_dispatch_notification_swallows_a_broker_failure_and_logs_it`
mirrors `test_dispatch_cascade.py` exactly, for the identical reason. Wiring tests confirm
each notification point fires from its REAL caller (`cancel_booking`, `create_offer_for_
freed_range`, `rematerialize_stale_series`) and that admin-cancellation does NOT fire on an
ordinary self-cancel.

Also runnable: `cd backend && ruff check . && ruff format --check . && mypy kairos` (all pass
with zero findings as of Phase 18). CI (`.github/workflows/ci.yml`) runs the CI tier as three
jobs — `lint`, `test`, `concurrency` (RECLAIM-04 excluded, see above) — on every PR. The spike
scripts under `scripts/spike/` are runnable but are diagnostic, not a test suite — see
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
  client doesn't send a zone today). **Re-defer, per this entry's own instruction: Phase 12
  passed without addressing it.** Phase 12's `BookingResponseSerializer`/booking-creation
  touch was `get_series_id` and the `series` field specifically — it built a
  `recurring_series`-facing timezone-storage path (the whole series definition, including its
  `timezone` field, travels through the `preview_token` and lands on `RecurringSeries`), but
  that is a genuinely different column for a genuinely different purpose (a series' OWN
  defining zone, not the zone a ONE-OFF booking happened to be created under) — building it
  did not, and structurally could not, also close this gap. **Still no phase in the 31-phase
  plan is explicitly scoped to add `booking.created_timezone`.** Per this entry's own
  fallback: carried forward to a dedicated polish/cleanup phase rather than assumed resolved.
  Must not be silently assumed to already exist, and must not be added incidentally as a side
  effect of unrelated work without updating this entry.

From Phase 12:

- **PRD FR16 ("editing a series definition must re-materialize future occurrences only") has
  no endpoint** — Phase 12's "Documents satisfied" line names it, but its own Scope IN never
  lists a series-edit endpoint at all, only preview/confirm/cancel. The CANCEL endpoint
  honors FR16's underlying "past occurrences are historical fact and immutable" principle for
  the one write it actually performs, but that is not the same as building FR16 itself. No
  phase in the current plan is explicitly scoped to add series editing — whichever phase
  needs it (most likely alongside Phase 13's re-materialization, which touches the same
  "recompute future occurrences from the definition" mechanism) should treat this as already
  flagged, not discover it fresh.
- **Recurring-series preview/confirm enforces neither the resource's bookable-hours/
  max-duration policy nor series-start-date past-dating — deliberately deferred, not merely
  overlooked.** Considered directly during Phase 12 and NOT added, for a reason stronger than
  "Spec v1.0 §5.8's 400-cause list doesn't mention them" (true, but not sufficient on its
  own — see the reason-on-cancel item below, which WAS a real gap and WAS closed on that
  reasoning alone). Bookable-hours specifically raises a genuine, unanswered design question:
  a resource's bookable window is defined in the RESOURCE's own timezone, which can differ
  from a series' own `timezone` field, so a correct check means converting each occurrence's
  UTC instant into `resource.timezone` and checking PER-OCCURRENCE (the resource's own DST
  can shift which occurrences pass, independently of the series' DST) — not the one
  series-level comparison of raw local times that would be "small." It also raises a
  behavioral question Spec doesn't answer: does a bookable-hours violation reject the whole
  preview (400, like single-booking creation), or surface per-occurrence like a conflict
  (consistent with FR10's "report precisely which occurrences failed and why")? Past-dating
  has the same shape of open question (reject the whole series, or only the past
  occurrences?). **Re-examined and NOT closed during Phase 13, on purpose.** This entry
  previously recommended Phase 13 as the owner; Phase 13 built the rolling-materialization
  MECHANISM but deliberately did not touch confirm's horizon-rejection behavior (see the next
  bullet — doing so would have broken REC-06, an already-passing, spec-literal test, and
  wasn't in Phase 13's own Scope IN). **Still no phase in the 31-phase plan is explicitly
  scoped to resolve this.** Whichever phase next revisits recurring-series creation should
  treat it as flagged, not rediscover it.
- **Phase 12's confirm still rejects a series whose occurrences extend beyond the 365-day
  horizon outright at 400, rather than materializing what fits now and leaving the rest for
  Phase 13's rolling-materialization job (PRD FR14c's literal design).** This means the
  rolling-materialization MECHANISM Phase 13 built (`kairos/bookings/tasks.py`) has NO REAL
  SERIES to act on today — proven directly against `RecurringSeries` rows constructed via the
  ORM instead (see Key Technical Decisions), the same "mechanism before its real caller"
  situation `actor_type='system'` was in from Phase 8 until Phase 13 gave it one. Deliberately
  NOT fixed in Phase 13: Test Plan REC-06 explicitly asserts "series extending beyond 365
  days → 400 validation_error," and that test is real, already merged, and passing — changing
  confirm's behavior would break a source-document-mandated test, not just an implementation
  detail, and wasn't in Phase 13's Scope IN regardless. **This is the SAME underlying gap as
  the bookable-hours/past-dating question above** — both require revisiting Phase 12's
  recurring-series creation validation together, and fixing one without the other would very
  likely need redoing. No phase in the current plan owns this; flag it, don't silently invent
  a fix under time pressure, the same discipline applied throughout this phase.

From Phase 18:

- **`notify_rollback_hold_released` has no real production caller.** Rollout v1.0 §4.5's hold-
  release procedure is a manual operational runbook (SQL an operator runs during an incident),
  not application code any phase in the 31-phase plan has built or is scoped to build. Per this
  phase's own explicit clarification, the notification TEMPLATE (distinct, non-generic wording
  — names the rollback explicitly, states queue position was preserved, deliberately avoids any
  "expire" framing) and the `NotificationService` send mechanism were built and are tested
  standalone, with a manually-constructed event — but no fake trigger path was invented just to
  give it a caller. **If a future phase (most plausibly Phase 30's go-live hardening, or a
  dedicated rollback-automation effort) ever builds real application code that performs a §4.5
  hold release, it should call `kairos.core.notifications.notify_rollback_hold_released`
  directly rather than inventing new messaging** — the wording was written specifically to
  satisfy §4.5's "must not be indistinguishable from an ordinary expiry" requirement, and a
  second, independently-invented message risks drifting from that.
- **PRD FR53's "or modified"** ("A user whose booking is cancelled OR MODIFIED by an
  administrator must be notified") has no corresponding notification, only the cancelled half —
  `edit_booking` has no admin-override code path at all (edit is owner-only, Spec v1.0 §5.5, no
  phase has added one), so there is nothing for an admin-modification notification to attach to
  yet. Not a gap in THIS phase's own scope (Phase 18's own Scope IN names only "admin
  cancellation with reason," not modification) — flagged here so a future phase that DOES add
  an admin-edit path doesn't miss that FR53 already expects a notification alongside it.

Genuine open questions from the source documents (offer window duration, nonexistent-time
policy default, series bounds, etc.) are tracked in PRD v1.0 §11 and RFC v1.0 §18; they get
resolved or explicitly deferred as the relevant phases are built.
