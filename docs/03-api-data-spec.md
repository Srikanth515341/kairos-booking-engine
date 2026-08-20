# API & Data Design Spec
## Concurrency-Safe Resource Booking Engine

| | |
|---|---|
| **Document version** | 1.0 |
| **Status** | Draft for Implementation |
| **Supersedes** | v0.1 (written against RFC v0.1) |
| **Builds on** | PRD v1.0 (approved), RFC v1.0 (approved, pending Spike S1) |
| **Contract for** | Frontend and backend, implemented independently |

---

## 0. Revision History

**v1.0 — changes from v0.1.** v0.1 was written against RFC v0.1 and implemented the waitlist design RFC v1.0 replaced.

| # | Change | Reason |
|---|---|---|
| 1 | `booking.status` gains `'held'`; constraint predicate widened to `status IN ('confirmed','held')`; EXCLUDE on `waitlist_offer` dropped; FK direction inverted so an offer references its hold. | v0.1 put the offer's exclusion constraint on a different table from bookings. Two constraints on two tables cannot exclude against each other — an offer and a booking could overlap freely. RFC v1.0 §15(c). |
| 2 | `idempotency_key` re-keyed to `(user_id, key)`; response columns made nullable; `status` column added; transaction boundary stated as a hard requirement; `request_in_progress` error code added. | v0.1's table could not represent an in-flight request, making concurrent replay (PRD FR36) unimplementable. Its single-column PK also let one user's key collide with another's. |
| 3 | `series_start_date` and `tzdata_version` added to `recurring_series`. | v0.1's API accepted `series_start_date` and its schema had nowhere to store it, making the series row non-authoritative and re-materialization (PRD FR13) impossible. |
| 4 | Bounds aligned to PRD: 100 occurrences (was 52), 365-day horizon (was unenforced), 92-day availability window (was 31). | v0.1 silently disagreed with its parent document. |
| 5 | Recurring creation split into preview + confirm. | PRD FR33 requires explicit user resolution of partial-series conflicts. v0.1 auto-created the non-conflicting subset. |
| 6 | `audit_log` table, triggers, session-variable propagation, append-only grants, and history endpoint added. | PRD FR39–43. Absent from v0.1 entirely. |
| 7 | Offboarding policy and endpoint added; Operations role added. | PRD FR44, FR49–51. Absent from v0.1. |
| 8 | Indexes corrected: partial GiST, hold-expiry index, `starts_at` generated column for sort support. Eligibility rule stated as containment. `service_unavailable` added for failover. `request_id` added throughout. | Review findings. |

## 1. Design Principles

- **Base path & versioning.** All routes under `/api/v1`. Rationale in §9.
- **Resource naming.** Plural nouns — `/resources`, `/bookings`, `/recurring-series`, `/waitlist-entries`, `/waitlist-offers`. Non-CRUD actions are `POST` sub-routes (`/bookings/{id}/cancel`), never overloaded verbs.
- **Timestamps.** Every wire timestamp is ISO 8601 with explicit UTC offset (`2026-09-01T13:00:00Z`). The server always returns UTC; local rendering is the frontend's responsibility (RFC §9). Instants use an `_at` suffix. Ranges are a start/end pair — JSON has no range type, and two instants are simpler to consume than Postgres range syntax. The API layer translates to and from `tstzrange`; that translation is a backend implementation detail.
- **IDs.** UUIDv4 in URLs and bodies. No sequential enumeration, and no ID-generation coordination between systems.
- **Auth.** Every endpoint requires `Authorization: Bearer <JWT>`, validated against the SSO/OIDC integration (RFC §4). No anonymous endpoints (PRD §5).
- **Authorization error convention.** Where an authenticated user lacks permission to view or act on a *specific instance* they don't own, the API returns 404, not 403 — this avoids confirming the instance exists (RFC §8.2, IDOR mitigation, applied at contract level). 403 is reserved for cases where the instance's existence is already known by context and only the *action* is gated (e.g. a non-admin editing a resource they can already GET). Each endpoint states which applies.
- **Request correlation.** Every response carries `X-Request-Id`. Clients may supply one; the server generates one otherwise. It propagates into the audit log (§4.7), making "why did this booking fail at 14:32" answerable.
- **Idempotency.** `Idempotency-Key` (client-generated UUIDv4) is required on every state-changing endpoint per PRD FR34. Full contract in §7.
- **Pagination.** Cursor-based on all list endpoints. Rationale in §8.
- **Content type.** `application/json` throughout.
- **Error shape.** One envelope across every endpoint. Full spec in §6.

## 2. Entity-Relationship Diagram

```
app_user (id PK, email, display_name, platform_role, status, deactivated_at, created_at)
  |
  |── created_by (1:M) ──► resource
  |── user_id     (1:M) ──► booking
  |── user_id     (1:M) ──► waitlist_entry
  |── created_by (1:M) ──► recurring_series
  |── M:N via resource_admin ──► resource
  v
resource (id PK, name, category, timezone, bookable_start_time, bookable_end_time,
          max_booking_duration_minutes, offboarding_policy, status, created_by FK, created_at)
  |
  |── (1:M) ──► booking
  |── (1:M) ──► waitlist_entry
  |── (1:M) ──► recurring_series
  v
recurring_series (id PK, resource_id FK, created_by FK, timezone, local_start_time,
                  local_end_time, weekday, occurrence_count, series_start_date,
                  tzdata_version, materialized_through, status, created_at)
  |
  |── series_id (1:M, nullable) ──► booking
  v
booking (id PK, resource_id FK, user_id FK, time_range TSTZRANGE, starts_at GENERATED,
         status {confirmed|held|cancelled}, expires_at, series_id FK NULL,
         created_at, cancelled_at, cancelled_by FK NULL, cancellation_reason)
  ▲                            ▲
  │                            │ hold_booking_id (1:0..1)
  │                            │
waitlist_entry (id PK, resource_id FK, user_id FK, time_range, status, joined_at)
  |                            │
  |── (1:M over time) ────────────────────► waitlist_offer (id PK, waitlist_entry_id FK,
                                              hold_booking_id FK UNIQUE, resource_id,
                                              time_range, status, expires_at, created_at)

idempotency_key (PK (user_id, key), endpoint, request_body_hash, status,
                 response_status NULL, response_body NULL, created_at, completed_at)

audit_log (id BIGSERIAL PK, entity_type, entity_id, action, actor_id, actor_type,
           reason, request_id, before_state JSONB, after_state JSONB, occurred_at)

system_check_run (id PK, check_name, run_at, status, findings JSONB)
```

**Cardinality notes**

- `app_user` 1—M `booking`, `waitlist_entry`, `resource`, `recurring_series`
- `app_user` M—N `resource` via `resource_admin`
- `recurring_series` 1—M `booking` (`series_id` is NULL for one-off bookings)
- `waitlist_entry` 1—M `waitlist_offer` *over time*, with at most one active at any moment (partial unique index). This is a change from v0.1, which made it 1—0..1 forever: a user who missed one offer became permanently ineligible on that entry even if the slot freed again the following week.
- `waitlist_offer` 1—1 `booking` via `hold_booking_id` — the offer points at its hold. The hold *is* a booking row with `status='held'`. This is the reservation. On acceptance, that same row transitions to `confirmed`; no new row is created.

## 3. Database Schema (DDL)

```sql
-- ============================================================
-- Extensions
-- ============================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS btree_gist; -- REQUIRED. Supplies a GiST operator class for
                     -- scalar types so `resource_id WITH =` can sit
                     -- alongside `time_range WITH &&` in one EXCLUDE
                     -- constraint. Without it, the constraint below
                     -- cannot be created and this design is void
                     -- (RFC §2.1, §3.4, Spike S1.1).

-- ============================================================
-- app_user
-- ============================================================
CREATE TABLE app_user (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,

    -- PRD FR44 defines four roles. Three are global and live here; the fourth
    -- (ResourceAdministrator) is inherently scoped and lives in resource_admin below.
    --   booker       - default; books for themselves
    --   system_admin - manages the resource catalogue and admin scope assignment
    --   operations   - read-only across resources, plus metrics and audit access
    platform_role TEXT NOT NULL DEFAULT 'booker'
                  CHECK (platform_role IN ('booker', 'system_admin', 'operations')),

    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'deactivated')),
    deactivated_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT deactivated_has_timestamp CHECK (
        (status = 'deactivated' AND deactivated_at IS NOT NULL) OR
        (status = 'active'      AND deactivated_at IS NULL)
    )
);
-- Authentication is delegated to the SSO/OIDC provider (RFC §4). This table is the local
-- identity record every foreign key resolves against.

-- ============================================================
-- resource
-- ============================================================
CREATE TABLE resource (
    id                            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                          TEXT NOT NULL,
    category                      TEXT NOT NULL DEFAULT 'meeting_room',
    timezone                      TEXT NOT NULL,  -- IANA identifier, e.g. 'Europe/Paris'.
                                                   -- A fixed offset ('+01:00') is rejected at
                                                   -- the API boundary (PRD FR8) because an
                                                   -- offset cannot express when rules change.
    bookable_start_time           TIME NOT NULL,  -- local wall-clock daily window
    bookable_end_time             TIME NOT NULL,
    max_booking_duration_minutes  INTEGER,        -- NULL = no cap

    -- PRD FR49. Applied to this resource's bookings when their owner is deactivated.
    offboarding_policy            TEXT NOT NULL DEFAULT 'transfer'
                                  CHECK (offboarding_policy IN
                                         ('transfer', 'cancel_and_notify', 'retain')),

    status                        TEXT NOT NULL DEFAULT 'active'
                                  CHECK (status IN ('active', 'inactive')),
    created_by                    UUID NOT NULL REFERENCES app_user(id),
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT bookable_window_valid CHECK (bookable_end_time > bookable_start_time)
);

-- Scoped administration (PRD FR45). Deliberately a grant table, not a boolean on app_user:
-- a facilities manager for Building A has no authority over Lab equipment.
CREATE TABLE resource_admin (
    resource_id UUID NOT NULL REFERENCES resource(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by  UUID NOT NULL REFERENCES app_user(id),
    PRIMARY KEY (resource_id, user_id)
);

-- ============================================================
-- recurring_series
-- ============================================================
CREATE TABLE recurring_series (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_id        UUID NOT NULL REFERENCES resource(id),
    created_by         UUID NOT NULL REFERENCES app_user(id),

    -- The series definition is AUTHORITATIVE; materialized occurrences in `booking` are
    -- DERIVED from it (RFC §9.2). Every field needed to recompute the full occurrence set
    -- from scratch must live here, or re-materialization (PRD FR13) is impossible.
    timezone           TEXT NOT NULL,        -- IANA identifier
    local_start_time   TIME NOT NULL,        -- wall-clock, NOT UTC
    local_end_time     TIME NOT NULL,
    weekday            SMALLINT NOT NULL CHECK (weekday BETWEEN 0 AND 6),  -- 0 = Sunday
    series_start_date  DATE NOT NULL,        -- local calendar date of occurrence #1
    occurrence_count   SMALLINT NOT NULL CHECK (occurrence_count BETWEEN 1 AND 100),
                                              -- PRD FR14a

    -- Which IANA tzdata release the current occurrences were materialized under. When the
    -- deployed tzdata version differs from this, occurrences for this series may be wrong
    -- and must be re-materialized (RFC §9.4). Without this column there is no way to know
    -- which series need recomputation after a rule change.
    tzdata_version     TEXT NOT NULL,

    materialized_through DATE NOT NULL,      -- rolling horizon watermark (PRD FR14c)
    status             TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'cancelled')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT series_window_valid CHECK (local_end_time > local_start_time)
);

-- Supports "which series need re-materialization after a tzdata change" (RFC §9.4).
CREATE INDEX idx_series_tzdata ON recurring_series (tzdata_version, timezone)
    WHERE status = 'active';

-- Supports the rolling materialization job (PRD FR14c).
CREATE INDEX idx_series_materialized_through ON recurring_series (materialized_through)
    WHERE status = 'active';

-- ============================================================
-- booking — the table the entire correctness guarantee lives on
-- ============================================================
CREATE TABLE booking (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_id    UUID NOT NULL REFERENCES resource(id),

    -- For status='confirmed', the owner. For status='held', the waitlisted user the slot is
    -- reserved FOR. Acceptance transitions this same row to 'confirmed' with the same
    -- user_id — no new row is created (RFC §10.3).
    user_id        UUID NOT NULL REFERENCES app_user(id),

    time_range     TSTZRANGE NOT NULL,
    starts_at      TIMESTAMPTZ GENERATED ALWAYS AS (lower(time_range)) STORED,
                   -- Materialized so list endpoints can sort and paginate on start
                   -- time with a plain btree index (§8) instead of a functional sort.

    -- 'held' is a real reservation, not bookkeeping: it occupies the SAME exclusion domain
    -- as a confirmed booking. This is what makes a waitlist offer enforceable (RFC §10.1).
    status         TEXT NOT NULL DEFAULT 'confirmed'
                   CHECK (status IN ('confirmed', 'held', 'cancelled')),
    expires_at     TIMESTAMPTZ,               -- set iff status='held'

    series_id      UUID REFERENCES recurring_series(id),  -- NULL for one-off bookings
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at   TIMESTAMPTZ,
    cancelled_by   UUID REFERENCES app_user(id),
    cancellation_reason TEXT,

    CONSTRAINT booking_time_range_not_empty CHECK (NOT isempty(time_range)),

    CONSTRAINT hold_has_expiry CHECK (
        (status = 'held' AND expires_at IS NOT NULL) OR
        (status <> 'held' AND expires_at IS NULL)
    ),

    -- ================================================================
    -- THE core correctness guarantee (RFC §2.1, §3; PRD §2).
    --
    -- No two rows with status IN ('confirmed','held') may exist for the same resource_id
    -- with overlapping time_range. Enforced unconditionally by Postgres on every INSERT and
    -- UPDATE. There is no corresponding application-level check anywhere in this system,
    -- by design.
    --
    -- The predicate includes 'held' deliberately. Holds are waitlist offers. If 'held' is
    -- removed from this predicate, a waitlist offer stops reserving anything and an
    -- ordinary user can take a slot out from under the person it was offered to — the
    -- system will still be "correct" and the waitlist promise will be broken.
    --
    -- If you are reading this because you are about to modify or drop this constraint
    -- during an unrelated migration: STOP. This constraint is not an optimization layered
    -- on application logic — it IS the correctness mechanism this project exists to
    -- provide. Removing it silently reintroduces the double-booking race the system was
    -- built to eliminate, and no test outside the concurrency suite will fail.
    --
    -- A scheduled production check asserts this constraint's existence and pages on its
    -- absence (PRD M3, §5.14 of this spec). That check exists because of this exact risk.
    -- ================================================================
    CONSTRAINT no_overlapping_bookings
        EXCLUDE USING gist (
            resource_id WITH =,
            time_range  WITH &&
        )
        WHERE (status IN ('confirmed', 'held'))
);

-- NOTE ON INDEXES: the EXCLUDE constraint above creates its own partial GiST index on
-- (resource_id, time_range) WHERE status IN ('confirmed','held'). That index serves the
-- availability-view read query directly (RFC §7.2) — one index serving both correctness
-- and reads. Do NOT add a second, non-partial GiST index on the same columns: it would be
-- redundant on the read path and would reintroduce cancelled rows into an index, discarding
-- the partial-index benefit described in RFC §3.5.

-- Hold reclamation sweep (RFC §10.4): "status='held' AND expires_at <= now()".
CREATE INDEX idx_booking_hold_expiry ON booking (expires_at)
    WHERE status = 'held';

-- "My bookings" list + cursor pagination on start time (§8).
CREATE INDEX idx_booking_user_starts ON booking (user_id, starts_at, id);

-- Series cancellation and re-materialization (PRD FR15, FR13).
CREATE INDEX idx_booking_series ON booking (series_id, starts_at)
    WHERE series_id IS NOT NULL;

-- ============================================================
-- waitlist_entry
-- ============================================================
CREATE TABLE waitlist_entry (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_id  UUID NOT NULL REFERENCES resource(id),
    user_id      UUID NOT NULL REFERENCES app_user(id),
    time_range   TSTZRANGE NOT NULL,
    status       TEXT NOT NULL DEFAULT 'waiting'
                 CHECK (status IN ('waiting','offered','fulfilled','expired','cancelled')),

    -- Server-set ONLY (DEFAULT now()); never accepted from a request payload. This is what
    -- makes FCFS ordering trustworthy — a client-supplied joined_at would let a user forge
    -- an earlier queue position (RFC §8.2).
    joined_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT wl_entry_time_range_not_empty CHECK (NOT isempty(time_range))
);

-- One live entry per user per resource per range. Includes 'offered' so a user cannot
-- re-join while already holding an outstanding offer (RFC §8.2).
CREATE UNIQUE INDEX uniq_live_waitlist_per_user_slot
    ON waitlist_entry (user_id, resource_id, time_range)
    WHERE status IN ('waiting', 'offered');

-- Eligibility lookup (PRD FR21): find entries whose requested range is FULLY CONTAINED BY
-- the freed range — `freed_range @> we.time_range`. Containment, not overlap. A GiST index
-- on the range serves @> as well as &&.
CREATE INDEX idx_waitlist_entry_lookup
    ON waitlist_entry USING gist (resource_id, time_range)
    WHERE status = 'waiting';

-- FCFS ordering within a resource (PRD FR22).
CREATE INDEX idx_waitlist_entry_order
    ON waitlist_entry (resource_id, joined_at, id)
    WHERE status = 'waiting';

-- ============================================================
-- waitlist_offer
-- ============================================================
CREATE TABLE waitlist_offer (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    waitlist_entry_id  UUID NOT NULL REFERENCES waitlist_entry(id),

    -- The hold that reserves the slot for this offer. This is a `booking` row with
    -- status='held'. It is what makes the offer enforceable: because the hold occupies the
    -- exclusion domain, no ordinary booking can take the slot while the offer stands
    -- (RFC §10.1). An offer without a hold is not permitted (PRD FR23).
    hold_booking_id    UUID NOT NULL UNIQUE REFERENCES booking(id),

    resource_id        UUID NOT NULL REFERENCES resource(id),  -- denormalized for query
    time_range         TSTZRANGE NOT NULL,                     -- convenience
    status             TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','confirmed','expired','declined')),
    expires_at         TIMESTAMPTZ NOT NULL,   -- mirrors booking.expires_at on the hold
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT wo_time_range_not_empty CHECK (NOT isempty(time_range))
);

-- NOTE: v0.1 of this spec carried an EXCLUDE constraint on this table to prevent two
-- workers offering the same freed slot. That constraint is GONE and must not be restored.
-- It solved offer-vs-offer collisions only, while leaving offer-vs-booking collisions
-- entirely unprotected — because two constraints on two different tables cannot exclude
-- against each other (RFC §15c). Mutual exclusion now lives in ONE place: the hold row in
-- `booking`. A second concurrent offer would require a second overlapping hold, which
-- no_overlapping_bookings already forbids (PRD FR25).

-- At most one active offer per entry at any moment; multiple offers over time are allowed.
CREATE UNIQUE INDEX uniq_active_offer_per_entry
    ON waitlist_offer (waitlist_entry_id)
    WHERE status = 'active';

CREATE INDEX idx_waitlist_offer_expiry ON waitlist_offer (expires_at)
    WHERE status = 'active';

-- ============================================================
-- idempotency_key
-- ============================================================
CREATE TABLE idempotency_key (
    -- Scoped to (user_id, key), NOT key alone. Two users independently generating the same
    -- UUID must not collide, and a key presented by a different principal must be treated
    -- as unseen — which also closes the key-harvesting threat in RFC §8.2.
    user_id             UUID NOT NULL REFERENCES app_user(id),
    key                 UUID NOT NULL,

    endpoint            TEXT NOT NULL,   -- e.g. 'POST /api/v1/bookings'
    request_body_hash   TEXT NOT NULL,   -- sha256 of the normalized request body

    -- 'in_progress' is written in the SAME transaction as the operation, before the outcome
    -- is known. This is what makes concurrent replay (PRD FR36) implementable: a retry
    -- arriving mid-flight blocks on this row's unique constraint rather than executing a
    -- second time. Response columns are therefore NULL until completion.
    status              TEXT NOT NULL CHECK (status IN ('in_progress', 'completed')),
    response_status     INTEGER,
    response_body       JSONB,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,

    PRIMARY KEY (user_id, key),

    CONSTRAINT completed_has_response CHECK (
        (status = 'completed'   AND response_status IS NOT NULL AND completed_at IS NOT NULL) OR
        (status = 'in_progress' AND response_status IS NULL)
    )
);

CREATE INDEX idx_idempotency_created ON idempotency_key (created_at);  -- 24h cleanup (§7)

-- ============================================================
-- audit_log (PRD FR39–FR43)
-- ============================================================
CREATE TABLE audit_log (
    id            BIGSERIAL PRIMARY KEY,
    entity_type   TEXT NOT NULL,    -- 'booking' | 'waitlist_entry' | 'waitlist_offer'
                                     -- | 'resource' | 'resource_admin' | 'app_user'
    entity_id     UUID NOT NULL,
    action        TEXT NOT NULL,    -- 'insert' | 'update' | 'delete'
    actor_id      UUID,             -- NULL only for writes that arrived with no actor set,
                                     -- which is a bug the reconciliation job alerts on
    actor_type    TEXT NOT NULL DEFAULT 'unknown'
                  CHECK (actor_type IN ('user','admin','system','unknown')),
    reason        TEXT,             -- REQUIRED for administrative overrides (PRD FR40)
    request_id    TEXT,             -- correlates to X-Request-Id (§1)
    before_state  JSONB,
    after_state   JSONB,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_entity  ON audit_log (entity_type, entity_id, occurred_at DESC);
CREATE INDEX idx_audit_actor   ON audit_log (actor_id, occurred_at DESC);
CREATE INDEX idx_audit_request ON audit_log (request_id);

-- APPEND-ONLY IS ENFORCED AT THE GRANT LEVEL, NOT IN APPLICATION CODE (PRD FR41).
-- The application role holds INSERT and SELECT only. No API path can modify history
-- because no API path *can*.
REVOKE UPDATE, DELETE ON audit_log FROM app_role;
GRANT  INSERT, SELECT ON audit_log TO   app_role;

-- Audit records are written by TRIGGERS, not by application code. Rationale (RFC §12):
-- an application-level audit is opt-in per code path, and the same future bulk-import
-- script that motivated choosing a database constraint over a distributed lock would also
-- skip an application-level audit write. A trigger cannot be skipped by any writer.
--
-- Triggers cannot see the authenticated principal, so the service layer sets
-- transaction-local session variables at the start of every write transaction:
--   SET LOCAL app.actor_id   = '<uuid>';
--   SET LOCAL app.actor_type = 'user' | 'admin' | 'system';
--   SET LOCAL app.reason     = '<text>';  -- required for admin overrides
--   SET LOCAL app.request_id = '<text>';
CREATE OR REPLACE FUNCTION write_audit_log() RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO audit_log (
        entity_type, entity_id, action, actor_id, actor_type, reason, request_id,
        before_state, after_state
    ) VALUES (
        TG_TABLE_NAME,
        COALESCE(NEW.id, OLD.id),
        LOWER(TG_OP),
        NULLIF(current_setting('app.actor_id', true), '')::UUID,
        COALESCE(NULLIF(current_setting('app.actor_type', true), ''), 'unknown'),
        NULLIF(current_setting('app.reason', true), ''),
        NULLIF(current_setting('app.request_id', true), ''),
        CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE to_jsonb(OLD) END,
        CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE to_jsonb(NEW) END
    );
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_booking        AFTER INSERT OR UPDATE OR DELETE ON booking
    FOR EACH ROW EXECUTE FUNCTION write_audit_log();
CREATE TRIGGER audit_waitlist_entry AFTER INSERT OR UPDATE OR DELETE ON waitlist_entry
    FOR EACH ROW EXECUTE FUNCTION write_audit_log();
CREATE TRIGGER audit_waitlist_offer AFTER INSERT OR UPDATE OR DELETE ON waitlist_offer
    FOR EACH ROW EXECUTE FUNCTION write_audit_log();
CREATE TRIGGER audit_resource       AFTER INSERT OR UPDATE OR DELETE ON resource
    FOR EACH ROW EXECUTE FUNCTION write_audit_log();
CREATE TRIGGER audit_resource_admin AFTER INSERT OR UPDATE OR DELETE ON resource_admin
    FOR EACH ROW EXECUTE FUNCTION write_audit_log();

-- ============================================================
-- system_check_run — results surface of the correctness monitors (PRD M2/M3, RFC §14)
-- ============================================================
CREATE TABLE system_check_run (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    check_name  TEXT NOT NULL CHECK (check_name IN
                 ('reconciliation','schema_assertion','hold_reaper','offer_cascade',
                  'series_materialization','tzdata_rematerialization')),
    run_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    status      TEXT NOT NULL CHECK (status IN ('pass','fail')),
    findings    JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_check_run_latest ON system_check_run (check_name, run_at DESC);
```

## 4. Concurrency-Critical SQL — Carry These Through Exactly

These are the statements RFC v1.0 specified precisely. Implement them as written; do not paraphrase.

### 4.1 Booking creation (RFC §4.4, §10.4, §11.2)

All of the following occur in one transaction:

```sql
BEGIN;
SET LOCAL app.actor_id = :actor_id;
SET LOCAL app.actor_type = 'user';
SET LOCAL app.request_id = :request_id;

-- (1) Claim the idempotency key. A concurrent replay blocks here on the PK.
INSERT INTO idempotency_key (user_id, key, endpoint, request_body_hash, status)
VALUES (:user_id, :key, :endpoint, :body_hash, 'in_progress');

-- (2) Reclaim expired holds for this resource/range. Narrow and indexed, not a scan.
--     This is what makes the system self-healing: a stalled reaper can never make a slot
--     permanently unbookable, because the next booker clears the stale hold (RFC §10.4).
DELETE FROM booking
 WHERE resource_id = :resource_id
   AND status = 'held'
   AND expires_at <= now()
   AND time_range && :time_range;

-- (3) The write. The EXCLUDE constraint decides. There is no application-level check.
INSERT INTO booking (resource_id, user_id, time_range, status)
VALUES (:resource_id, :user_id, :time_range, 'confirmed');

-- (4) Record the outcome in the SAME transaction. If this were a separate transaction, a
--     crash between (3) and (4) would leave the booking committed and the key absent — and
--     the retry would return "slot unavailable" for the user's own booking (PRD FR38).
UPDATE idempotency_key
   SET status = 'completed', response_status = 201, response_body = :body,
       completed_at = now()
 WHERE user_id = :user_id AND key = :key;
COMMIT;
```

Session settings required on the write path (RFC §7.1): `lock_timeout = '3s'`, `statement_timeout = '10s'`, `idle_in_transaction_session_timeout = '30s'`. Isolation is `READ COMMITTED` — sufficient because the constraint is enforced at write time independent of isolation.

Blocking is expected. A conflicting insert against an *uncommitted* competitor waits rather than failing immediately. On `lock_timeout` (55P03), return 503 `service_unavailable` with `Retry-After`, not a 409 — the outcome is unknown, not decided.

### 4.2 Offer creation (RFC §10.2)

```sql
-- Eligibility: the freed range must FULLY CONTAIN the entry's requested range (PRD FR21).
-- Containment (@>), not overlap (&&). Partial overlap does not qualify in v1.
SELECT id, user_id, time_range
  FROM waitlist_entry
 WHERE resource_id = :resource_id
   AND status = 'waiting'
   AND :freed_range @> time_range
 ORDER BY joined_at ASC, id ASC
 LIMIT 1;

-- Create the hold. If this fails with 23P01, something already occupies the range —
-- a race with a direct booking, or another worker. Re-query and try the next candidate.
INSERT INTO booking (resource_id, user_id, time_range, status, expires_at)
VALUES (:resource_id, :entry_user_id, :entry_range, 'held', now() + :offer_window);

INSERT INTO waitlist_offer (waitlist_entry_id, hold_booking_id, resource_id,
                             time_range, expires_at)
VALUES (:entry_id, :hold_id, :resource_id, :entry_range, :expires_at);

UPDATE waitlist_entry SET status = 'offered' WHERE id = :entry_id;
```

### 4.3 Offer acceptance (RFC §10.3)

```sql
-- Conditional update on a single row. Postgres serializes concurrent updates to the same
-- row, so the outcome is decided by whichever transaction commits first.
UPDATE booking
   SET status = 'confirmed', expires_at = NULL
 WHERE id = :hold_booking_id
   AND status = 'held'
   AND user_id = :user_id
   AND expires_at > now();

-- 0 rows affected -> the offer is no longer valid -> 409 offer_expired
-- 1 row  affected -> confirmed. Because the hold already occupied the exclusion domain,
--                    this CANNOT lose a race to a direct booking (RFC §10.3).

UPDATE waitlist_offer SET status = 'confirmed' WHERE id = :offer_id AND status = 'active';
UPDATE waitlist_entry SET status = 'fulfilled' WHERE id = :entry_id;
```

No new booking row is created. The hold *is* the booking. It changes status.

### 4.4 Hold reclamation (RFC §10.4)

Two mechanisms, both required:

1. **Cleanup-on-write** — §4.1 step (2). Guarantees an expired hold never permanently blocks a booking, independent of worker health.
2. **Reaper** — periodic sweep (30s initial), expires holds and cascades to the next eligible waitlist entry. Required because cascade must fire even when *nobody is trying to book*.

The race between them and acceptance is safe in both orderings: if cleanup commits first, §4.3's conditional update matches zero rows; if acceptance commits first, the row is confirmed with `expires_at` NULL, so cleanup's `WHERE status='held' AND expires_at <= now()` matches zero rows.

## 5. API Endpoints

### 5.1 Create booking

`POST /api/v1/bookings` — Auth: required. Headers: `Idempotency-Key: <uuid>` (required).

```json
{
  "resource_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "start": "2026-09-01T13:00:00Z",
  "end":   "2026-09-01T14:00:00Z"
}
```

**201 Created**

```json
{
  "id": "b1c2d3e4-...",
  "resource_id": "3fa85f64-...",
  "user_id": "9a8b7c6d-...",
  "start": "2026-09-01T13:00:00Z",
  "end":   "2026-09-01T14:00:00Z",
  "status": "confirmed",
  "series_id": null,
  "created_at": "2026-08-19T10:15:00Z"
}
```

| Code | Status | Cause |
|---|---|---|
| `validation_error` | 400 | `end <= start`; outside bookable window; exceeds max duration; `start` in the past; beyond the 365-day advance horizon (PRD FR14b) |
| `unauthorized` | 401 | missing/invalid JWT |
| `not_found` | 404 | resource doesn't exist, or `status='inactive'` |
| `slot_unavailable` | 409 | `no_overlapping_bookings` rejected the insert (SQLSTATE 23P01). A confirmed booking *or an active hold* may be the blocker — the response does not distinguish, to avoid leaking waitlist state |
| `request_in_progress` | 409 | an earlier request with this key is still executing (§7) |
| `idempotency_key_conflict` | 422 | same key, different body |
| `rate_limited` | 429 | per-principal limit exceeded |
| `service_unavailable` | 503 | `lock_timeout` exceeded, or primary failover. Carries `Retry-After`. The outcome is unknown — retry with the same key (PRD M14) |

### 5.2 Get booking

`GET /api/v1/bookings/{id}` — Permission: owner, or resource admin for its resource, or operations. Otherwise 404. 200 OK: shape as 5.1. Failures: 401; 404.

### 5.3 Booking history (audit) — PRD FR42

`GET /api/v1/bookings/{id}/history` — Permission: same as 5.2.

```json
{
  "booking_id": "b1c2d3e4-...",
  "events": [
    { "occurred_at": "2026-08-19T10:15:00Z", "action": "insert",
      "actor": { "id": "9a8b...", "display_name": "Alex Chen", "type": "user" },
      "reason": null, "request_id": "req_01H...",
      "changes": { "status": [null, "confirmed"] } },
    { "occurred_at": "2026-08-20T08:02:00Z", "action": "update",
      "actor": { "id": "1f2e...", "display_name": "Facilities Bot", "type": "admin" },
      "reason": "Room offline for maintenance", "request_id": "req_01H...",
      "changes": { "status": ["confirmed", "cancelled"] } }
  ]
}
```

Answers "what happened to my booking?" months later. Failures: 401; 404.

### 5.4 List bookings

`GET /api/v1/bookings?resource_id=&status=&time=upcoming|past|all&limit=&cursor=`

Without `resource_id`, returns only the requester's own. With `resource_id`, requester must be a resource admin or operations, otherwise 403 (the resource is browsable, so its existence isn't secret — §1 convention).

Holds are never returned by this endpoint. A `status='held'` row is a reservation, not a booking, and surfaces only through §5.11.

200 OK: `{"data": [...], "next_cursor": "..."}`. Failures: 401; 403.

### 5.5 Edit booking

`PATCH /api/v1/bookings/{id}` — Headers: `Idempotency-Key` required (PRD FR34). Permission: owner only. Otherwise 404.

Body: `{"start": "...", "end": "..."}`. Only the range is editable; `resource_id` is not — book a different resource by cancelling and creating, which keeps semantics unambiguous.

Evaluated against `no_overlapping_bookings` exactly as a create (PRD FR5). An EXCLUDE constraint checked on UPDATE compares the new row against every *other* row, so editing a booking's own range in place does not self-conflict.

200 OK: updated booking. Failures: 400; 401; 404; 409 `slot_unavailable`; 409 `request_in_progress`; 422; 503.

### 5.6 Cancel booking

`POST /api/v1/bookings/{id}/cancel` — Headers: `Idempotency-Key` required. Permission: owner, or resource admin (override, PRD FR47). Otherwise 404.

```json
{ "reason": "Room needed for facilities maintenance" }
```

`reason` is required when the requester is not the owner (PRD FR40, FR53 — the notified user must learn why); optional for self-cancellation.

200 OK: booking with `status: "cancelled"` and cancellation fields populated. Cancelling an already-cancelled booking returns 200 with existing state, not an error — "make sure this is cancelled" achieves its intent either way.

The row leaves the exclusion domain (`confirmed → cancelled`, PRD FR4) and the waitlist check is enqueued via `transaction.on_commit()` after this response is sent. This response never contains offer information — there is no synchronous coupling between cancellation and any resulting offer.

Failures: 400 (missing reason on override); 401; 404; 409 `request_in_progress`; 422.

### 5.7 Resource availability

`GET /api/v1/resources/{id}/availability?from=2026-09-01&to=2026-09-30` — any authenticated user.

Range capped at 92 days (PRD FR30). Beyond that → 400.

**200 OK**

```json
{
  "resource_id": "3fa85f64-...",
  "timezone": "Europe/Paris",
  "range": { "from": "2026-09-01T00:00:00Z", "to": "2026-09-30T00:00:00Z" },
  "as_of": "2026-08-19T10:14:59Z",
  "data_freshness": "replica",
  "busy_blocks": [
    { "start": "2026-09-01T13:00:00Z", "end": "2026-09-01T14:00:00Z",
      "booking_id": "b1c2...", "owner": { "id": "9a8b...", "display_name": "Alex Chen" } },
    { "start": "2026-09-02T09:00:00Z", "end": "2026-09-02T10:00:00Z" }
  ]
}
```

- `booking_id` and `owner` appear only when the requester owns that booking or administers the resource. Otherwise omitted entirely, not nulled — field-level authorization (RFC §8.2).
- Held slots appear as ordinary busy blocks with no identifying fields. A hold makes a slot genuinely unavailable, and exposing "this is held for someone on the waitlist" would leak queue state.
- `data_freshness` is `"replica"` or `"primary"`. If replica lag exceeds threshold, the server serves from primary and reports `"primary"` (PRD FR31).

This endpoint is advisory, not authoritative (PRD FR29). §10 covers the frontend consequence.

Failures: 400 (`to` before `from`; range > 92 days); 401; 404.

### 5.8 Preview recurring series — PRD FR33

`POST /api/v1/bookings/recurring/preview` — Auth: required. No idempotency key (this endpoint commits nothing).

```json
{
  "resource_id": "3fa85f64-...",
  "timezone": "Europe/Paris",
  "local_start_time": "10:00:00",
  "local_end_time":   "10:30:00",
  "weekday": 2,
  "series_start_date": "2026-09-01",
  "occurrence_count": 8
}
```

`weekday`: 0 = Sunday … 6 = Saturday. `timezone` must be an IANA identifier; a fixed offset is rejected (PRD FR8). Expansion is server-side only — never trust a client to expand a recurrence rule (RFC §9.2).

**200 OK**

```json
{
  "preview_token": "pv_01HQ...",
  "resource_id": "3fa85f64-...",
  "tzdata_version": "2026a",
  "would_create": [
    { "occurrence_date": "2026-09-01", "start": "2026-09-01T08:00:00Z", "end": "2026-09-01T08:30:00Z" },
    { "occurrence_date": "2026-10-27", "start": "2026-10-27T09:00:00Z", "end": "2026-10-27T09:30:00Z" }
  ],
  "conflicts": [
    { "occurrence_date": "2026-09-15", "start": "2026-09-15T08:00:00Z",
      "end": "2026-09-15T08:30:00Z", "reason": "slot_unavailable" }
  ],
  "time_adjustments": [
    { "occurrence_date": "2027-03-28", "issue": "nonexistent_local_time",
      "requested_local": "02:30:00", "adjusted_local": "03:30:00",
      "explanation": "Local time does not exist due to a DST transition; shifted forward by the transition gap." }
  ]
}
```

Note the UTC instants differ across the October DST boundary while local time stays 10:00 — each occurrence is independently converted from local wall-clock time, never derived by adding a fixed duration to the previous one (RFC §9.2).

`time_adjustments` surfaces nonexistent (PRD FR11) and ambiguous (PRD FR12) local times. The system never guesses silently.

`preview_token` is valid 15 minutes and is required by §5.9.

Failures: 400 (invalid weekday/timezone; `local_end_time <= local_start_time`; `occurrence_count` outside 1–100; horizon exceeded); 401; 404.

### 5.9 Confirm recurring series

`POST /api/v1/bookings/recurring` — Headers: `Idempotency-Key` required.

```json
{
  "preview_token": "pv_01HQ...",
  "acknowledged_conflicts": ["2026-09-15"],
  "acknowledged_adjustments": ["2027-03-28"]
}
```

The client must explicitly acknowledge every conflict and adjustment from the preview. This is PRD FR33: a series quietly missing an occurrence the booker never noticed is the same failure class this project exists to eliminate — the user believes they hold something they do not.

**207 Multi-Status**

```json
{
  "series_id": "c4d5e6f7-...",
  "created": [
    { "booking_id": "b1...", "occurrence_date": "2026-09-01", "start": "...", "end": "..." }
  ],
  "conflicts": [
    { "occurrence_date": "2026-09-15", "reason": "slot_unavailable", "acknowledged": true },
    { "occurrence_date": "2026-09-22", "reason": "slot_unavailable", "acknowledged": false }
  ]
}
```

207, not 201: this produces per-occurrence outcomes, not one pass/fail. Each occurrence is attempted in its own transaction (RFC §5d) — all-or-nothing would let one contested Tuesday block all eight weeks and would hold locks across the resource for the whole series write.

`acknowledged: false` means the occurrence conflicted *between* preview and confirm — a real possibility, since the preview is advisory like every other read. §10 covers rendering.

| Code | Status | Cause |
|---|---|---|
| `validation_error` | 400 | malformed body |
| `preview_expired` | 409 | token older than 15 minutes — re-preview |
| `unacknowledged_conflicts` | 409 | preview reported conflicts or adjustments the body did not acknowledge (PRD FR33) |
| `request_in_progress` | 409 | — |
| `idempotency_key_conflict` | 422 | — |

No request-level `slot_unavailable` — per-occurrence conflicts live in the 207 body.

### 5.10 Cancel recurring series

`POST /api/v1/recurring-series/{id}/cancel` — Headers: `Idempotency-Key`. Permission: `created_by`, or resource admin. Otherwise 404.

Cancels every still-confirmed future occurrence (`starts_at >= now()`) under this `series_id` (PRD FR15). Past occurrences remain as historical confirmed records — cancelling a series cancels what's left of it, not history.

200 OK: `{"series_id": "...", "cancelled_booking_ids": [...], "occurrences_already_past": 1}`

### 5.11 Join waitlist

`POST /api/v1/waitlist-entries` — Headers: `Idempotency-Key` (PRD FR34).

Body: `{"resource_id": "...", "start": "...", "end": "..."}`

201 Created: `{"id": "...", "resource_id": "...", "start": "...", "end": "...", "status": "waiting", "joined_at": "..."}`

Eligibility rule the client must understand (PRD FR21): this entry will be offered a freed slot only if the freed range *fully contains* this requested range. Partial overlap does not qualify in v1. A user waitlisting 10:00–11:00 will *not* be offered a freed 10:00–10:30.

| Code | Status | Cause |
|---|---|---|
| `validation_error` | 400 | malformed range |
| `not_found` | 404 | resource doesn't exist |
| `already_on_waitlist` | 409 | live entry exists for this user/resource/range (`uniq_live_waitlist_per_user_slot`) |
| `slot_already_available` | 422 | the range is currently free — book it directly. Advisory: this is a check-then-act and the slot may be taken by the time you act on it |

### 5.12 List my waitlist entries

`GET /api/v1/waitlist-entries?status=&limit=&cursor=` — always scoped to the requester. There is no `user_id` parameter, by design, so there is no permission check to get wrong.

```json
{
  "data": [
    { "id": "e5f6...", "resource_id": "...", "start": "...", "end": "...",
      "status": "offered", "joined_at": "...", "queue_position": 1,
      "active_offer": { "id": "f1a2...", "expires_at": "2026-08-19T10:35:00Z" } }
  ],
  "next_cursor": null
}
```

`queue_position` satisfies PRD FR27. `active_offer` present only when `status: "offered"`.

### 5.13 Confirm / decline waitlist offer

`POST /api/v1/waitlist-offers/{id}/confirm` — Headers: `Idempotency-Key`. Permission: the entry's owner. Otherwise 404.

Executes §4.3 exactly. 201 Created: the booking (the hold row, now confirmed), with `waitlist_offer_id` populated.

| Code | Status | Cause |
|---|---|---|
| `offer_expired` | 409 | the conditional update matched zero rows — expired before this request landed. This is expected under ordinary network latency; the client countdown is not authoritative |
| `not_found` | 404 | doesn't exist / isn't this user's |
| `request_in_progress` | 409 | — |

There is no `slot_unavailable` on this endpoint. v0.1 listed one as "belt and suspenders." It cannot occur: the hold already occupies the exclusion domain, so acceptance cannot lose to a direct booking. If it ever fires, the hold mechanism is broken and it is an incident, not a user-facing error.

`POST /api/v1/waitlist-offers/{id}/decline` — same permission. Releases the hold immediately and cascades to the next eligible entry sooner than expiry would. 200 OK: offer with `status: "declined"`. Failures: 401; 404; 409 `offer_already_resolved`.

### 5.14 Resource administration

| Endpoint | Permission | Notes |
|---|---|---|
| `POST /api/v1/resources` | `system_admin` | 403 if not — the capability is gated, not any instance's existence |
| `GET /api/v1/resources` | any authenticated | paginated (§8) |
| `GET /api/v1/resources/{id}` | any authenticated | 404 if absent |
| `PATCH /api/v1/resources/{id}` | resource admin or `system_admin` | Updatable: name, bookable window, max duration, `offboarding_policy`, status. Non-admin → 403 (visible via GET, so existence isn't the protected thing). Setting `status:"inactive"` takes a resource offline. No DELETE endpoint — referential integrity and audit history for bookings that reference it |
| `POST /api/v1/resources/{id}/admins` | `system_admin` or existing resource admin | Body `{"user_id": "..."}`. 409 if already granted |
| `DELETE /api/v1/resources/{id}/admins/{user_id}` | same | Revokes scope |

### 5.15 Operations & lifecycle

`GET /api/v1/admin/checks/latest` — Permission: `operations` or `system_admin`.

```json
{
  "checks": [
    { "check_name": "schema_assertion",       "last_run_at": "...", "status": "pass" },
    { "check_name": "reconciliation",         "last_run_at": "...", "status": "pass",
      "findings": { "overlaps_found": 0 } },
    { "check_name": "hold_reaper",            "last_run_at": "...", "status": "pass" },
    { "check_name": "offer_cascade",          "last_run_at": "...", "status": "pass" },
    { "check_name": "series_materialization", "last_run_at": "...", "status": "pass" },
    { "check_name": "tzdata_rematerialization","last_run_at": "...", "status": "pass" }
  ]
}
```

Read-only surface over `system_check_run`. The jobs query Postgres directly; this endpoint exists so dashboards don't need database access.

On reconciliation failure, the alert text must state what it means (RFC §14): if the constraint is present, that query is structurally incapable of returning a row. A hit means the guarantee has been removed — a dropped constraint, a restore without it, or out-of-band writes. It is not a race detector. An on-call engineer who reads a hit as "a race occurred" will investigate the wrong thing under pressure.

`GET /api/v1/admin/resources/{id}/utilization?from=&to=` — Permission: resource admin, operations, or `system_admin`.

```json
{ "resource_id": "...", "range": {...}, "total_bookings": 42, "total_booked_minutes": 2100,
  "waitlist_joins": 6, "cancellation_count": 3, "offers_confirmed": 4, "offers_expired": 2 }
```

`POST /api/v1/admin/users/{id}/deactivate` — Permission: `system_admin`. Headers: `Idempotency-Key`. PRD FR49–51.

```json
{ "reason": "Employee offboarding — ticket HR-4821" }
```

**200 OK**

```json
{
  "user_id": "...",
  "status": "deactivated",
  "bookings_transferred": 4,
  "bookings_cancelled": 0,
  "bookings_retained": 2,
  "waitlist_entries_cancelled": 3,
  "offers_released": 1,
  "series_flagged_for_admin": [
    { "series_id": "...", "resource_id": "...", "occurrences_remaining": 6 }
  ]
}
```

Per-resource `offboarding_policy` decides transfer / cancel / retain. Outstanding holds are released so the slot cascades rather than expiring uselessly (PRD FR50). Recurring series are flagged to the resource admin (PRD FR51). All actions audited with `actor_type='system'`.

## 6. Error Format Standard

```json
{
  "error": {
    "code": "slot_unavailable",
    "message": "This time slot is no longer available.",
    "details": {},
    "request_id": "req_01HQ..."
  }
}
```

`details` carries error-specific structure — for `validation_error`, the offending field: `{"field": "end", "issue": "must be after start"}`. `request_id` appears on every error so a user can quote it to support and it resolves against the audit log.

| Code | Status | Meaning |
|---|---|---|
| `validation_error` | 400 | failed shape or policy validation |
| `unauthorized` | 401 | missing or invalid auth |
| `permission_denied` | 403 | authenticated; the action is gated (not the instance's existence) |
| `not_found` | 404 | doesn't exist, or exists and the requester has no right to know that |
| `slot_unavailable` | 409 | `no_overlapping_bookings` rejected the write |
| `already_on_waitlist` | 409 | duplicate live waitlist entry |
| `offer_expired` | 409 | acceptance lost the race against expiry |
| `offer_already_resolved` | 409 | decline on an already-confirmed/expired offer |
| `request_in_progress` | 409 | an earlier request with this key is still executing |
| `preview_expired` | 409 | recurring preview token older than 15 min |
| `unacknowledged_conflicts` | 409 | confirm body didn't acknowledge preview conflicts/adjustments |
| `slot_already_available` | 422 | waitlist join on a currently-free slot |
| `idempotency_key_conflict` | 422 | same key, different body |
| `rate_limited` | 429 | limit exceeded |
| `service_unavailable` | 503 | lock timeout or primary failover — outcome unknown, carries `Retry-After` |

### 6.1 The two mappings that matter

`slot_unavailable` ⟵ SQLSTATE `23P01`. This code exists specifically to represent an `exclusion_violation` surfacing at the API boundary. `BookingService` catches *that specific SQLSTATE* — never a generic exception handler, which would conflate a real conflict-prevention event with an unrelated database error such as a foreign-key violation. No other condition in this system produces this code. If you see it, the exclusion constraint fired.

`request_in_progress` vs. `slot_unavailable` — never conflate these. Both are 409 and they mean opposite things. `slot_unavailable`: someone else holds this slot. `request_in_progress`: *your own* earlier request is still running. Returning `slot_unavailable` for an in-flight replay would tell a user their own booking made the slot unavailable — precisely the misinformation the idempotency mechanism exists to prevent (PRD FR38). Distinct codes, distinct messages, distinct client handling.

## 7. Idempotency — Concrete Contract

Coverage (PRD FR34): `POST /bookings`, `PATCH /bookings/{id}`, `POST /bookings/{id}/cancel`, `POST /bookings/recurring`, `POST /waitlist-entries`, `POST /waitlist-offers/{id}/confirm`, `POST /admin/users/{id}/deactivate`. Not required on GET or on `/preview` (commits nothing).

*v0.1 exempted cancel and confirm on the reasoning that conditional updates make them naturally repeatable. That reasoning is sound for the state transition itself but does not cover the response: a retried confirm whose first response was lost must return the original booking, not re-evaluate. The key is required.*

1. **First use.** Key absent → an `in_progress` row is inserted in the same transaction as the operation (§4.1). On completion, the same transaction updates it to `completed` with the status and body. One transaction, both writes — this is the entire design (RFC §11.2). A separate transaction leaves a window where the booking committed and the key didn't, producing exactly the failure this prevents.
2. **Replay, same key + same body.** The stored response is returned verbatim; the side effect is not re-executed. Response carries `Idempotent-Replay: true`.
3. **Replay while the original is in flight.** The retry blocks on the primary key, then finds `status='in_progress'` → 409 `request_in_progress`, never `slot_unavailable` (§6.1). If the original completes first, the retry returns its stored response as case 2.
4. **Replay, same key + different body.** Hash mismatch → 422 `idempotency_key_conflict`. A client bug signal; never silently accepted under either body.
5. **Different principal, same key.** Treated as unseen — keys are scoped `(user_id, key)`. Closes the key-harvesting threat (RFC §8.2).
6. **Retention: 24 hours**, then purged (`idx_idempotency_created`). This is the concrete default the contract needs; RFC §18 left tuning open. A browser retry after a dropped connection happens within seconds to minutes, not days. Cleanup-job growth must be monitored (PRD R5). After 24h a reused key is a brand-new request.
7. **Conflict outcomes are recorded too.** A 409 is a legitimate final outcome; a retry must receive the same 409, not a fresh attempt.

## 8. Pagination, Filtering, Sorting

Cursor-based, not offset, on every list endpoint. Offset pagination is unsafe on a concurrently-written list — which describes every list here. A row inserted or removed between page fetches shifts every subsequent offset, causing skipped or duplicated rows. A cursor anchored on a stable sort key has no such failure mode.

- Request: `?limit=20&cursor=<opaque_base64>`. Default 20, cap 100.
- Response: `{"data": [...], "next_cursor": "<opaque>" | null}`
- Cursor contents (opaque to clients): base64 of `(sort_key_value, id)`. The `id` tiebreak guarantees stable ordering when sort keys collide.

| Endpoint | Default sort | Backing index |
|---|---|---|
| Bookings (5.4) | `starts_at ASC` (upcoming) / `DESC` (past) | `idx_booking_user_starts` (user_id, starts_at, id) |
| Waitlist entries (5.12) | `joined_at ASC` — matches the actual FCFS queue position | `idx_waitlist_entry_order` |
| Resources (5.14) | `name ASC` | btree on (name, id) |

*Note the `starts_at` generated column (§3) exists specifically so this sort is index-backed rather than a per-page sort on `lower(time_range)`.*

Filters are plain query parameters and are not encoded into the cursor — changing a filter always starts a fresh first page.

## 9. Versioning

URL path versioning: `/api/v1/...`. Chosen over header/content-negotiation because it is explicit and cacheable (no `Vary` interaction to reason about) and needs no negotiation parsing on either side — a real simplicity win when frontend and backend are built independently against this document.

Non-breaking changes — new optional request fields, new response fields, new endpoints — ship within v1 with no bump. A client built against this document today must not break from a future addition it doesn't know about.

Breaking changes get `/api/v2/...` alongside v1. v1 serves through a deprecation window (minimum 6 months), with `Sunset` (RFC 8594) on v1 responses once v2 exists — not a surprise cutover.

## 10. Consistency Notes for Frontend Implementation

- **409 `slot_unavailable` on booking creation is expected, normal behavior** — not a bug and not a backend error. The availability view is a best-effort snapshot, not a lock (PRD FR29), so a 409 on submit is an anticipated outcome of this design. Frontend error tracking must exclude 409 `slot_unavailable` from anything that pages an on-call engineer — otherwise the team gets paged for the system working exactly as designed.
- **Optimistic UI with rollback** is the intended pattern, not merely tolerated. Render as pending on submit; finalize on 201; on 409, roll back, show the specific conflict message, and re-fetch availability so the user sees current state rather than the stale view that led them there.
- **503 `service_unavailable` is not a failure** — it is "unknown, retry with the same key." Do not show "booking failed." Retry with the identical `Idempotency-Key`; the server returns the original outcome if it committed. Showing a failure here, when the booking may have succeeded, is the exact ambiguity PRD M14 exists to eliminate.
- **`request_in_progress` (409) is not `slot_unavailable` (409).** Same status code, opposite meanings. `request_in_progress` → wait briefly and retry with the same key. `slot_unavailable` → the slot is gone; offer alternatives. Branch on `error.code`, never on the HTTP status.
- **The recurring flow is two steps and the preview is mandatory.** Preview → show conflicts and time adjustments → user acknowledges → confirm. Do not auto-acknowledge to skip a click: PRD FR33 requires the user to actually see what will not be created. `time_adjustments` in particular — a DST-shifted occurrence — must be shown, not hidden.
- **207 with a non-empty `conflicts` array is a success, not a partial failure to retry.** Render exactly which occurrences succeeded and which didn't (PRD FR10). Do not collapse a 207 into a generic error state. An entry with `acknowledged: false` conflicted *between preview and confirm* and deserves distinct wording — "this slot was taken while you were confirming."
- **Missing `booking_id`/`owner` in availability means unauthorized, not missing data.** Render as an opaque busy block. Do not attempt a separate fetch — it will 404 for the identical reason.
- **A held slot looks like any other busy slot.** Do not attempt to distinguish or label it; the API deliberately does not expose hold state, to avoid leaking waitlist queue information.
- **Offer `expires_at` is a UX aid, not the source of truth.** Expiry is enforced server-side by a conditional update, so a countdown can reach `/confirm` a moment before hitting zero and still legitimately receive 409 `offer_expired` through ordinary network latency. Handle as expected — "this offer has expired," refresh the entry — not as an error state.
- **Generate the `Idempotency-Key` once per user action** and reuse it across every automatic retry — never a fresh UUID per attempt. A retry loop that mints a new key defeats the entire contract in §7. The key is bound to "the user clicked Book," not to "an HTTP request was sent."
- **Send `X-Request-Id` and surface it on errors.** It is what makes a support conversation resolvable against the audit log.

## 11. Open Items Carried Forward

- **Offer window duration** (PRD open question 1). 15 minutes assumed throughout. It is simultaneously the hold duration, so it directly determines how long a resource is unbookable by everyone else — not merely a UX parameter.
- **Rate-limit thresholds** (RFC §18). Endpoint exists; numbers pending real usage.
- **Per-resource policy fields** — max advance window, minimum cancellation notice (PRD open question 7). The schema accommodates them as additional resource columns; not specified because the PRD hasn't committed to v1 inclusion. The 365-day horizon is currently system-wide.
- **tzdata_rematerialization conflict resolution.** v1 surfaces conflicts for human resolution (PRD FR13b); no API for resolving them is specified here because the resolution workflow is undesigned.
- **Spike S1 dependency.** If `btree_gist` is unavailable on the target platform (S1.1), the EXCLUDE constraints in §3 cannot be created and this schema is void. RFC Candidate D would become the approach and this spec would be substantially rewritten. Verify before implementation.

*End of document.*
