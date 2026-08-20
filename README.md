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

🏁 **Milestone 1 reached.** The core guarantee is proven under genuine concurrency: 200
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
no DST at all. No API surface yet — this is the engine only; Phase 12 wires it up to real
endpoints. See
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

No frontend yet — see Status above and [`CLAUDE.md`](CLAUDE.md).

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
| Recurrence expansion engine | **Live** — DST-safe, per-occurrence, no API surface yet (Phase 11) |
| Recurring bookings (API, re-materialization) | Not started (Phase 12–13) |
| Enforceable waitlist | Not started (Phase 14–17) |
| Notifications | Not started (Phase 18) |
| Admin & offboarding | Not started (Phase 19) |
| Production correctness monitoring | Not started (Phase 20–21) |
| Frontend | Not started (Phase 23–27) |
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
