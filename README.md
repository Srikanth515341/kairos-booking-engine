# Kairos — Concurrency-Safe Resource Booking Engine

*Kairos* (καιρός) — the Greek word for the opportune moment. A booking system's entire job
is deciding who gets a moment when two people want the same one.

## What this is

Booking systems look like simple CRUD until two users try to reserve the same room for
overlapping times at the same instant. Most homegrown systems have a race window: the
application checks whether a slot is free, then writes the booking — and between those two
steps, a second concurrent request can also see the slot as free and also write. Both
requests are individually correct; the bug lives in the gap between them.

**Kairos closes that gap at the database, not in application code.** No two overlapping
bookings for the same resource can ever both succeed, because the guarantee is enforced by
a PostgreSQL `EXCLUDE` constraint on `(resource_id, time_range)` — not by a check-then-insert
sequence that some future code path could get wrong. The database itself makes an
overlapping write impossible, regardless of which endpoint, script, or migration
initiates it.

On top of that guarantee, Kairos also handles the parts that are easy to get wrong even
once correctness is solved:

- **A waitlist that actually reserves the slot.** An offer isn't a promise — it's a `held`
  row occupying the *same* exclusion domain as a real booking, so an ordinary user genuinely
  cannot take a slot out from under someone who was offered it.
- **DST-correct recurring bookings.** A weekly meeting stores local wall-clock time plus an
  IANA timezone, not a fixed UTC offset — so it doesn't silently drift an hour every time
  the clocks change.
- **Retry-safe writes.** Every state-changing request carries an idempotency key, written in
  the same transaction as the booking itself, so a network retry can never tell a user their
  own successful booking is unavailable.
- **Production-verifiable correctness.** A standing reconciliation check and a schema
  assertion run continuously, because the failure mode of a removed guarantee is silence,
  not an error.

This project is built end-to-end following a full engineering documentation pipeline — PRD,
RFC/Technical Design Doc, API & Data Spec, Test Plan, Rollout & Runbook, and a phased
Implementation Plan — all committed under [`docs/`](docs/).

## Status

🏁 **Milestone 3 reached** (`v0.3.0-milestone-3-waitlist-enforceable`) — the waitlist is fully
enforceable end to end: join, hold, offer, accept, decline, cascade, all backed by the same
constraint that protects an ordinary booking. 🏁 **Milestone 2** (`v0.2.0-milestone-2-recurrence-
dst-correct`) — recurring bookings work end to end, DST-correct, through a real two-step HTTP
flow. 🏁 **Milestone 1** — the
core guarantee proven under genuine concurrency: 200
independently-connected clients, released simultaneously against one identical slot,
exactly one succeeds — verified 10 consecutive times and running in CI on every commit
(Phase 3). Repository scaffolding and the six planning documents are in place (Phase 0);
the architectural bet — a PostgreSQL exclusion constraint as the correctness mechanism —
was verified against a real PostgreSQL 16 instance (Phase 1 spike; see
[`docs/spikes/S1-postgres-verification.md`](docs/spikes/S1-postgres-verification.md)); the
Django project and core schema exist (Phase 2): `app_user`, `resource`, `resource_admin`,
and `booking`, with the `no_overlapping_bookings` `EXCLUDE` constraint enforced at the
database level. `POST /api/v1/bookings` is live (Phase 4) with policy validation, correct
SQLSTATE-to-HTTP translation on every write-path outcome, and structured logging — and
retry-safe (Phase 5): every write carries an idempotency key, claimed in the same
transaction as the booking itself, so a network retry can never tell a user their own
successful booking is unavailable. The read path is live too (Phase 6): booking detail/list
with cursor pagination, and resource availability with field-level authorization —
`booking_id`/`owner` are omitted entirely, not nulled, unless you own the booking or
administer the resource. Bookings can now be edited and cancelled (Phase 7):
`PATCH /api/v1/bookings/{id}` is evaluated against the exclusion constraint exactly like a
create, and `POST /api/v1/bookings/{id}/cancel` supports an owner self-cancel or a
resource-admin override with a required reason — cancelling an already-cancelled booking is a
200 no-op, not an error. Every state transition is now audited too (Phase 8): a database
trigger — not application code — writes an immutable record on every insert/update/delete to
`booking`/`resource`/`resource_admin`, so even a raw SQL write that bypasses the API entirely
still gets recorded, and the running application connects as a least-privilege database role
that can `INSERT`/`SELECT` its own audit log but structurally cannot `UPDATE` or `DELETE` it —
enforced by Postgres grants, not a promise in application code. `GET /bookings/{id}/history`
surfaces the full trail. Authentication is real now too (Phase 9): OIDC-issued tokens
exchanged for a short-lived session token, validated on every request — no more dev-only
header stub outside the test suite. Four roles (booker, scoped resource administrator, global
system administrator, read-only operations) are enforced through one authorization service, an
admin's authority never extends past the specific resource they're scoped to, and a resource
can be restricted to a group whose non-members can't even tell it exists. The timezone
foundation is in place too (Phase 10): every timestamp is `timestamptz`/UTC end to end, a
single `local_to_instant` conversion utility takes the occurrence's own date as authoritative
so a booking created under one DST regime for an occurrence in another can never pick up the
wrong offset, a fixed UTC offset submitted as a timezone is rejected outright, and the pinned
`tzdata` version is logged on every startup and asserted in CI. Recurring series can now be
expanded correctly across a DST boundary too (Phase 11): each occurrence is computed in local
wall-clock time first and converted to UTC using the rules in effect on its *own* date, never
by adding a fixed weekly duration to the previous occurrence's UTC instant — the naive
approach that silently drifts by an hour after every DST transition. A nonexistent local time
(a spring-forward gap) is detected and shifted forward with the adjustment disclosed; an
ambiguous one (a fall-back overlap) resolves to the first, pre-transition instant. Verified
against Europe/Paris, America/New_York, and Australia/Sydney (whose DST runs in the opposite
calendar direction, catching sign errors the northern-hemisphere zones can't) and a zone with
no DST at all. That engine is wired up to real endpoints now (Phase 12):
`POST /bookings/recurring/preview` computes a full series and reports conflicts and DST
adjustments without writing anything, and `POST /bookings/recurring` requires the caller to
explicitly acknowledge every one of them before creating a single row — a series quietly
missing an occurrence the booker never noticed is exactly the failure class this project
exists to eliminate. Each occurrence commits in its own independent transaction, so one
contested week never blocks the other seven or holds a lock across the whole resource; a
conflict that arises between preview and confirm is reported distinctly from one the booker
already knew about. The confirm response is idempotent byte-for-byte, not just once-per-key —
a replay returns the identical outcome rather than re-evaluating anything. A recurring series
can be cancelled going forward without touching its history. Background workers exist now too
(Phase 13): Celery + Redis, running as `worker`/`beat` services alongside Postgres under the
same `docker compose up`. A tzdata rule change doesn't just silently mis-render a stored
instant — the re-materialization job recomputes every affected occurrence straight from its
series definition, updates the ones that changed, and never drops one that conflicts with a
booking made in the interim; it's flagged for the series owner and the resource administrator
instead, and the rest of the series still succeeds. Every background write carries its own
audit attribution too — `actor_type: system`, not a fabricated human actor. Users can now join
a waitlist too (Phase 14): `POST /waitlist-entries` inserts directly, no availability
pre-check — the same philosophy as booking creation — with 409 when you're already on it
(covering an outstanding offer too, not just another plain entry) and 422 when the range is
already fully bookable and you should just book it. Eligibility is defined as *containment*,
not overlap: a freed 10:00–11:00 makes a 10:00–11:00 waitlister eligible, a freed 10:00–10:30
does not, since their full requested range wasn't actually freed. `GET /waitlist-entries`
reports your live queue position; `POST /waitlist-entries/{id}/cancel` withdraws. And a
waitlist offer now genuinely reserves something (Phase 15): a hold is an ordinary row in the
SAME `booking` table, in the SAME `status IN ('confirmed','held')` exclusion domain a
confirmed booking occupies — so an outstanding offer cannot be taken out from under the
person it was made to, not by another user's booking, not under concurrent load. 50
independently-connected clients released simultaneously against an actively held range: zero
succeed, every time. And the whole loop closes now (Phase 16): cancelling a booking really
does dispatch a background worker that finds the next eligible waitlister, reserves the slot
with a hold, and creates the offer — the hold always exists before the offer does, so there's
never a moment where an offer is live but nothing is actually holding the range for it.
`POST /waitlist-offers/{id}/confirm` accepts an offer atomically (the hold becomes the
confirmed booking, in place — no second row); `POST /waitlist-offers/{id}/decline` releases it
immediately and cascades to the next person in line, sooner than the offer would otherwise
expire. And an unanswered hold can no longer block anything forever (Phase 17): two
independent mechanisms reclaim it, because a database constraint can't express "now" and
neither mechanism alone is enough. Every booking write clears any expired hold in its own way
first, so a stalled background worker — or Redis being completely down — can never make a
resource permanently unbookable; a periodic sweep separately expires holds and moves the
waitlist along even when nobody happens to be booking anything at that moment. Verified against
a real outage, not a simulated one: with Redis actually stopped, booking creation and
cancellation still succeed, a booking over an expired hold still succeeds, and the only thing
that degrades is exactly the one thing that should — no new offer goes out until Redis is back.
And every one of those events now actually reaches someone (Phase 18): a waitlist offer states
its expiry explicitly, an administrative cancellation includes the reason, and a timezone-rule
change tells you exactly how your recurring time moved — all dispatched from a background
worker, never from inside the request that triggered them, so a slow or failing email provider
can never turn a successful cancellation into an apparent request failure. A delivery failure
is retried with exponential backoff and recorded, not silently dropped or silently retried
forever. Resources now have a real admin surface too (Phase 19): create, update, and delegate
admin scope over HTTP, with no delete endpoint at all — a resource goes offline by
`status: "inactive"`, never by disappearing, so bookings that reference it keep their history
intact. Offboarding a user applies a real, per-resource policy to every booking they hold —
transferred, cancelled with notice, or retained for manual review — releases any slot they were
sitting on so it reaches the next person waiting rather than expiring uselessly, and locks their
session out immediately, not just their ability to create new bookings. And now the central
guarantee is provably still true in production, not just in a one-time spike (Phase 20): a
scheduled schema check re-derives the exclusion constraint's exact definition — not merely
whether it exists — hourly and on every deploy, so a predicate quietly narrowed during an
unrelated migration is caught before anyone is ever double-booked; a second, independent check
looks for the double-booking directly and is structurally incapable of finding one as long as
the constraint holds. Both are readable through one endpoint, and neither check invented its own
notion of "overlap" — the reconciliation query reuses the exact range operator the constraint
itself is built on. Every one of those checks now actually pages someone, too (Phase 21): a
failing or stale check fires a real alert email, once per incident rather than once per poll,
and a live operations dashboard — one JSON endpoint plus one self-contained HTML page, no
Grafana required — shows all six checks, every open alert, and request-level metrics (P95
latency, 503s split by cause, auth failures by shape) computed live from real traffic. And the
one endpoint most exposed to abuse is rate-limited now (Phase 22): a Redis-backed token bucket
per authenticated user, deliberately a fairness policy and not a correctness guarantee — it
fails open if Redis is unavailable, because an infrastructure hiccup in a rate limiter must
never block a real booking. Injection resistance is proven, not assumed: a static scan confirms
every raw SQL statement in the codebase is parameterized, and security headers plus an explicit,
no-wildcard-ever CORS allowlist are real response headers, verified as such, not just settings
that look right. And there's finally something to point a browser at (Phase 23): a React +
TypeScript SPA with real login (against the same dev-mock-login → token-exchange flow the curl
walkthrough below uses by hand), protected routes, and an API client that gets the two subtle
contracts right on purpose — one idempotency key per user action, reused across every automatic
retry, and a `503` treated as "retry, don't fail" rather than shown to the user as an error.
The primary user experience now exists on top of that shell (Phase 24): a day/week/month
calendar rendered in the viewer's own local timezone (shown explicitly), booking creation with
optimistic UI (a slot renders as taken the instant you click "Book," before the network round
trip completes), and a real, specific answer when someone else books the same slot first — "Someone
booked this a moment ago. Here are the nearest open slots," never a bare "booking failed" — plus
cancellation, editing, and a My Bookings list with real cursor pagination. A held slot and
another user's confirmed booking render identically, on purpose — the same opacity the backend
itself enforces, not a frontend rule.
See
[`CLAUDE.md`](CLAUDE.md) for exactly what is and isn't built, and
[`docs/06-implementation-plan.md`](docs/06-implementation-plan.md) for the full 31-phase
build plan.

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python, Django, Django REST Framework |
| Database | PostgreSQL 16 + `btree_gist` |
| Background jobs | Celery + Redis |
| Frontend | TypeScript, React |
| Auth | OIDC / SSO |
| CI | GitHub Actions |

## Architecture (planned)

```
             [React SPA]
                  |
              HTTPS / JSON
                  |
                  v
  [Identity/SSO] <--OIDC--> [Django + DRF API service]
                                |       |        \
                    SQL writes  |       | reads    \ enqueue (on_commit)
                   via PgBouncer|       |            \
                                v       v             v
                 [PostgreSQL PRIMARY]   |       [Redis — Celery broker]
                  · booking             |               |
                    (EXCLUDE, partial)  |               v
                  · idempotency_key     |         [Celery workers]
                  · audit_log           |          · offer + cascade
                  · waitlist_entry      |          · hold reclamation
                        |               |          · series materialization
                streaming |             |          · tz re-materialization
                replication|            |          · reconciliation / schema check
                        v               |          · notification dispatch
                 [PostgreSQL REPLICA] <-+                  |
                  (availability reads,                     v
                   lag-monitored)              [Transactional email provider]
```

Full rationale for every decision in this diagram is in
[`docs/02-rfc.md`](docs/02-rfc.md).

## Setup and installation

**Database (PostgreSQL via Docker):**

```bash
cd infra
docker compose up -d
docker exec kairos_postgres psql -U kairos -d kairos_dev -c "SELECT 1;"
```

**Backend:**

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate      # Windows; `source .venv/bin/activate` on macOS/Linux
pip install -e ".[dev]"

# Migrations need DDL privileges the app's own least-privilege kairos_app
# role deliberately doesn't have (Phase 8) — override DATABASE_URL to the
# superuser DSN for this ONE command:
DATABASE_URL=postgresql://kairos:kairos@localhost:5432/kairos_dev python manage.py migrate

python manage.py runserver  # now connects as kairos_app by default
```

**Frontend (Phase 23; Calendar & booking flow — Phase 24):**

```bash
cd frontend
npm install
cp .env.example .env       # defaults already point at http://127.0.0.1:8000/api/v1
npm run dev                # http://localhost:5173
```

The backend must allow the frontend's origin explicitly (`kairos.core.middleware.
CorsMiddleware` — Phase 22, no wildcard, ever): run the backend with
`CORS_ALLOWED_ORIGINS=http://localhost:5173` set (add it to `backend/.env`, or export it
before `manage.py runserver`). Sign in at `http://localhost:5173/login` using the "dev
sign-in" form (any email — this calls the same `POST /auth/dev-mock-login` →
`POST /auth/token` round trip the curl walkthrough below does by hand). "Sign in with SSO"
is present but structurally untested against a live IdP — see `frontend/src/auth/oidc.ts`'s
own docstring and CLAUDE.md's Open Questions. Once signed in, **Calendar** shows real
availability for whichever resource is selected (needs at least one `status='active'`
resource in the database — the `POST /resources` curl example further below creates one) and
**My Bookings** lists your own bookings with cursor pagination; click an empty slot to book,
a booked slot you own to cancel or edit. To see the conflict-handling behavior for real, open
the app in two browser tabs signed in as two different users (two different emails at the
dev sign-in form) and try booking the exact same slot from both.

**Try it** — auth is real now (Phase 9): a session token, obtained via an OIDC login, on
every request. Locally there's no real identity provider to log in against, so
`POST /auth/dev-mock-login` mints a token from a fixed local keypair standing in for one —
exchange it for a session token the same way a real ID token would be:

```bash
# 1. Mock IdP issues an ID token (dev/test only — never available in prod)
ID_TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/dev-mock-login \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "display_name": "You"}' | python -c "import sys,json;print(json.load(sys.stdin)['id_token'])")

# 2. Exchange it for this backend's own short-lived session token — this
#    step is the SAME one a real OIDC login goes through.
ACCESS_TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/token \
  -H "Content-Type: application/json" \
  -d "{\"id_token\": \"$ID_TOKEN\"}" | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
```

Logging in auto-provisions your `AppUser` — grab its id for the resource fixture below:

```bash
python manage.py shell -c "
from datetime import time
from kairos.identity.models import AppUser
from kairos.resources.models import Resource
user = AppUser.objects.get(email='you@example.com')
resource = Resource.objects.create(name='Room 1', timezone='UTC',
    bookable_start_time=time(0,0), bookable_end_time=time(23,59), created_by=user)
print(resource.id)
"

curl -i -X POST http://127.0.0.1:8000/api/v1/bookings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Idempotency-Key: $(python -c 'import uuid; print(uuid.uuid4())')" \
  -d '{"resource_id": "<resource-id-from-above>", "start": "2026-09-01T13:00:00Z", "end": "2026-09-01T14:00:00Z"}'
```

`Idempotency-Key` is required (missing it is 400) — generate a fresh UUID per user action
and reuse it across retries of that SAME action, never a new one per HTTP attempt. Replay
the exact same request (same key, same body) and you'll get the original booking back with
an `Idempotent-Replay: true` header, not a 409.

Read it back, list your bookings, and check availability:

```bash
curl -s http://127.0.0.1:8000/api/v1/bookings -H "Authorization: Bearer $ACCESS_TOKEN" | python -m json.tool

curl -s "http://127.0.0.1:8000/api/v1/resources/<resource-id-from-above>/availability?from=2026-09-01&to=2026-09-08" \
  -H "Authorization: Bearer $ACCESS_TOKEN" | python -m json.tool
```

Edit or cancel it (`Idempotency-Key` is required on both, same as creation):

```bash
curl -i -X PATCH http://127.0.0.1:8000/api/v1/bookings/<booking-id-from-above> \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Idempotency-Key: $(python -c 'import uuid; print(uuid.uuid4())')" \
  -d '{"start": "2026-09-01T15:00:00Z", "end": "2026-09-01T16:00:00Z"}'

curl -i -X POST http://127.0.0.1:8000/api/v1/bookings/<booking-id-from-above>/cancel \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Idempotency-Key: $(python -c 'import uuid; print(uuid.uuid4())')" \
  -d '{}'
```

See who did what and why — every insert/update/delete above was recorded by a database
trigger, not application code, so this reflects the truth even for a write that skipped the
API entirely:

```bash
curl -s http://127.0.0.1:8000/api/v1/bookings/<booking-id-from-above>/history \
  -H "Authorization: Bearer $ACCESS_TOKEN" | python -m json.tool
```

Join a waitlist (Phase 14) for a range that's already booked — joining a range with no
conflict at all gets 422 `slot_already_available`, since you should just book it directly:

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/waitlist-entries \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Idempotency-Key: $(python -c 'import uuid; print(uuid.uuid4())')" \
  -d '{"resource_id": "<resource-id-from-above>", "start": "2026-09-01T13:00:00Z", "end": "2026-09-01T14:00:00Z"}'

curl -s http://127.0.0.1:8000/api/v1/waitlist-entries \
  -H "Authorization: Bearer $ACCESS_TOKEN" | python -m json.tool

curl -i -X POST http://127.0.0.1:8000/api/v1/waitlist-entries/<entry-id-from-above>/cancel \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Idempotency-Key: $(python -c 'import uuid; print(uuid.uuid4())')"
```

Create a resource and delegate admin scope over it (Phase 19 — `system_admin` only; every prior
resource in this walkthrough was created directly via `manage.py shell`, this is the first real
endpoint for it):

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/resources \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -d '{"name": "Room 2", "timezone": "UTC", "bookable_start_time": "09:00:00", "bookable_end_time": "17:00:00"}'

curl -i -X POST http://127.0.0.1:8000/api/v1/resources/<resource-id-from-above>/admins \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -d '{"user_id": "<some-user-id>"}'
```

A frontend now exists (Phase 23), and the primary booking flow — calendar, booking,
cancel/edit, My Bookings — is real (Phase 24) — see the **Frontend** step above. Resource
CRUD and admin screens are still curl-only; see Status above and [`CLAUDE.md`](CLAUDE.md).

## Running the test suite

**This is the command that matters most in this repository.** 200 independently-connected
clients (their own threads, their own psycopg connections — never a shared pool), released
simultaneously via a `threading.Barrier` against one identical time slot, with production
write-path session settings applied. Asserts exactly one succeeds, verified against ground
truth in the database, not just response codes — 10 consecutive times:

```bash
cd backend
pytest tests/concurrency -v
```

This is Milestone 1: the project's central claim, proven under genuine concurrency, running
in CI on every commit as its own named `concurrency` job. If you're reviewing this project,
that's the command to run first.

The full suite, including the schema-level smoke test and the schema-assertion check that
fails the moment anyone narrows the `EXCLUDE` predicate:

```bash
cd backend
pytest
```

`ruff check . && ruff format --check . && mypy kairos` also passes with zero findings.

**Frontend (Phase 23; Calendar & booking flow — Phase 24):**

```bash
cd frontend
npm run typecheck && npm run lint && npm run format:check && npm run test && npm run build
```

## Feature status

| Feature | Status |
|---|---|
| Core exclusion-constraint guarantee | **Proven under concurrency — Milestone 1** (Phase 1–3) |
| Booking creation | **Live** — `POST /api/v1/bookings` (Phase 4) |
| Booking edit / cancel | **Live** — `PATCH`/`POST .../cancel`, CONC-03/04 proven under concurrency (Phase 7) |
| Idempotent writes | **Live** — required `Idempotency-Key` on every mutation (Phase 5, 7) |
| Booking detail / list, resource list / detail / availability | **Live** — cursor pagination, field-level authorization (Phase 6) |
| Audit trail | **Live** — trigger-based, append-only by database grant, `GET /bookings/{id}/history` (Phase 8) |
| Auth & scoped authorization | **Live** — real OIDC session tokens, four roles, scoped resource admin, restricted resources (Phase 9) |
| Timezone foundation | **Live** — UTC-only API, IANA validation, DST-safe conversion + detection utilities (Phase 10) |
| Recurrence expansion engine | **Live** — DST-safe, per-occurrence (Phase 11) |
| Recurring bookings API | **Live** — preview/confirm/cancel, 207 Multi-Status, per-occurrence transactions (Phase 12) 🏁 Milestone 2 |
| Background workers (Celery + Redis) | **Live** — `worker`/`beat` under `docker compose up`, verified end to end (Phase 13) |
| Rolling / tzdata re-materialization | **Live** — mechanism proven directly; no real caller yet, see CLAUDE.md (Phase 13) |
| system_check_run / correctness-monitor records | **Live** — `series_materialization`/`tzdata_rematerialization` only; full six-check alerting is Phase 21 |
| Waitlist join / list / cancel | **Live** — containment eligibility query (PRD FR21), no offer cascade caller yet (Phase 14) |
| Holds occupy the exclusion domain | **Live** — proven under concurrency (HOLD-01/02/03) (Phase 15) |
| Waitlist offers — creation, cascade, accept, decline | **Live** — hold-before-offer (PRD FR23), atomic accept, decline-and-cascade (WL-01/02/03) 🏁 Milestone 3 (Phase 16) |
| Hold reclamation (reaper + cleanup-on-write) | **Live** — both RFC §10.4 mechanisms, verified against a real Redis outage (RECLAIM-01–04, WL-05/06) (Phase 17) |
| Notifications | **Live** — offer/admin-cancellation/re-materialization dispatch, async-only, retried with backoff and recorded (PRD FR52–55) (Phase 18) |
| Resource admin & offboarding | **Live** — resource CRUD, admin-scope grants, utilization, per-resource-policy user deactivation (OFF-01/02) (Phase 19) |
| Reconciliation & schema assertion | **Live** — both checks scheduled + on-deploy, `GET /admin/checks/latest`, RECON-01–08 (Phase 20) |
| Alert routing, metrics dashboard | **Live** — `evaluate_alerts` fires/resolves real email alerts, `GET /admin/dashboard` + HTML page, RECON-07 (Phase 21) |
| Security hardening | **Live** — Redis token-bucket rate limiting, injection-resistance evidence (SEC-04), security headers, explicit CORS (SEC-01–07) (Phase 22) |
| Frontend foundation | **Live** — React + TS SPA, OIDC/dev-mock login, protected routes, API client with idempotency-key retry semantics (Phase 23) |
| Calendar & booking flow | **Live** — day/week/month calendar in the viewer's local timezone, optimistic booking creation, specific conflict messaging + nearest-open-slot suggestions, cancel/edit, My Bookings with cursor pagination 🏁 Milestone 4 (Phase 24) |
| Deployed / live | Not started (Phase 30) |

## Documentation

| Document | Purpose |
|---|---|
| [`docs/01-prd.md`](docs/01-prd.md) | Product Requirements — what and why |
| [`docs/02-rfc.md`](docs/02-rfc.md) | Technical Design — architecture decisions and tradeoffs |
| [`docs/03-api-data-spec.md`](docs/03-api-data-spec.md) | API contract and database schema |
| [`docs/04-test-plan.md`](docs/04-test-plan.md) | Adversarial concurrency tests and acceptance criteria |
| [`docs/05-rollout-runbook.md`](docs/05-rollout-runbook.md) | Staged rollout, rollback plan, incident runbooks |
| [`docs/06-implementation-plan.md`](docs/06-implementation-plan.md) | The 31-phase build plan being executed |

## License

MIT — see [`LICENSE`](LICENSE).
