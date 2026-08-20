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

No application code yet. `infra/docker-compose.yml` runs PostgreSQL 16 with `btree_gist`
enabled and `max_connections=600` (spike/test setting — see the file's comment). Everything
else in `scripts/spike/` is throwaway spike code (RFC §16) that produced
`docs/spikes/S1-postgres-verification.md` and does not become the application.

Planned structure (populated phase by phase, per RFC §4.1):

```
kairos-booking-engine/
├── .github/workflows/    # CI (skeleton now, real jobs from Phase 3)
├── docs/                 # the six source documents + spike reports, checklists
├── backend/
│   ├── kairos/           # Django project — empty until Phase 2
│   ├── tests/
│   └── migrations/
├── frontend/              # React + TypeScript — empty until Phase 23
├── infra/                 # docker-compose.yml — exists (Postgres only; Redis added Phase 13)
└── scripts/
    └── spike/             # throwaway S1 spike scripts — will NOT be extended after Phase 1
```

## Completed Phases

| Phase | Name | One-line summary | Merged |
|---|---|---|---|
| 0 | Repository & Process Foundation | Scaffolding, six docs committed, CLAUDE.md/README.md initialized, CI skeleton | Pending (direct-to-main commit) |
| 1 | Spike S1 — Postgres Verification | All of RFC §16 S1.1–S1.7 verified against real PostgreSQL 16; gate PASSED; Candidate A confirmed. One liveness finding carried forward (see below) | Pending (on branch `phase-01-spike-postgres-verification`) |

## Current Phase In Progress

None. Phase 1 is complete pending review and merge. Phase 2 (Core Schema & The Exclusion
Constraint) is next.

## NOT Yet Built

No Django project, no application-level database schema (only the throwaway spike table
`spike_booking` exists, in the dev database, created/dropped repeatedly by
`scripts/spike/common.py`), no API, no frontend, no CI jobs beyond a placeholder, no tests
beyond the spike scripts. Do not assume any of these exist in a fresh session — verify
against this file and `git log` first.

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

## Running Locally

Postgres only, for now:

```bash
cd infra
docker compose up -d
docker exec kairos_postgres psql -U kairos -d kairos_dev -c "SELECT 1;"
```

Django app setup starts Phase 2; the frontend starts Phase 23.

## Running Tests

Not yet applicable to the application (no app exists). The spike scripts under
`scripts/spike/` are runnable but are diagnostic, not a test suite — see
`docs/spikes/S1-postgres-verification.md` for what each one does and its recorded output.
The real concurrency test suite (the project's central proof) is introduced in Phase 3.

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
