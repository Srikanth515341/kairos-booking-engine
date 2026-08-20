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
database level. `POST /api/v1/bookings` is live (Phase 4) — the first user-reachable
surface, with policy validation, correct SQLSTATE-to-HTTP translation on every write-path
outcome, and structured logging. No idempotency yet (Phase 5), no read/edit/cancel
endpoints (Phase 6–7), and auth is a dev-only stub (Phase 9 does the real thing). See
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
python manage.py migrate    # DATABASE_URL defaults to the docker-compose credentials above
python manage.py runserver
```

**Try it** — auth is a dev-only stub (`X-Dev-User-Id`, Phase 9 replaces it with real
OIDC/SSO), so create a user and resource first:

```bash
python manage.py shell -c "
from datetime import time
from kairos.identity.models import AppUser
from kairos.resources.models import Resource
user = AppUser.objects.create(email='you@example.com', display_name='You')
resource = Resource.objects.create(name='Room 1', timezone='UTC',
    bookable_start_time=time(0,0), bookable_end_time=time(23,59), created_by=user)
print(user.id, resource.id)
"

curl -i -X POST http://127.0.0.1:8000/api/v1/bookings \
  -H "Content-Type: application/json" \
  -H "X-Dev-User-Id: <user-id-from-above>" \
  -d '{"resource_id": "<resource-id-from-above>", "start": "2026-09-01T13:00:00Z", "end": "2026-09-01T14:00:00Z"}'
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
| Booking edit / cancel | Not started (Phase 7) |
| Idempotent writes | Not started (Phase 5) |
| Audit trail | Not started (Phase 8) |
| Auth & scoped authorization | Not started (Phase 9) |
| DST-correct recurring bookings | Not started (Phase 10–13) |
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
