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

🚧 **Early construction.** This repository currently contains only the Phase 0 scaffolding:
project structure, the six planning documents, and process configuration. No application
code exists yet. See [`CLAUDE.md`](CLAUDE.md) for exactly what is and isn't built, and
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

Not yet applicable — no application code exists. This section will be filled in starting
Phase 2 (database + Docker Compose) and Phase 4 (API).

## Running the test suite

Not yet applicable. The centerpiece of this project's test suite is a **concurrency stress
test**: 200 independently-connected clients, released simultaneously via a synchronization
barrier against the same contested time slot, asserting that exactly one succeeds — run 100
consecutive times before release. It is introduced in Phase 3 and will be documented here
as a single, highlighted command once it exists. If you're reviewing this project, that's
the command to run first.

## Feature status

| Feature | Status |
|---|---|
| Core exclusion-constraint guarantee | Not started (Phase 1–3) |
| Booking creation / edit / cancel | Not started (Phase 4–7) |
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
