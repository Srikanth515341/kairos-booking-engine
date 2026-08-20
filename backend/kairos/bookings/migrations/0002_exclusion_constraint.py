from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations

# ============================================================
# Verbatim from Spec v1.0 §3 — the comment block is not decoration. Rollout
# RUNBOOK-01 cause #1 is someone narrowing this predicate during an unrelated
# migration, and this comment, reproduced at the point of definition, is a
# primary mitigation (Implementation Plan §1.3, "load-bearing comment
# sites").
# ============================================================
ADD_CONSTRAINT_SQL = """
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
ALTER TABLE booking ADD CONSTRAINT no_overlapping_bookings
    EXCLUDE USING gist (
        resource_id WITH =,
        time_range  WITH &&
    )
    WHERE (status IN ('confirmed', 'held'));

-- NOTE ON INDEXES: the EXCLUDE constraint above creates its own partial GiST index on
-- (resource_id, time_range) WHERE status IN ('confirmed','held'). That index serves the
-- availability-view read query directly (RFC §7.2) — one index serving both correctness
-- and reads. Do NOT add a second, non-partial GiST index on the same columns: it would be
-- redundant on the read path and would reintroduce cancelled rows into an index, discarding
-- the partial-index benefit described in RFC §3.5.
"""

DROP_CONSTRAINT_SQL = "ALTER TABLE booking DROP CONSTRAINT no_overlapping_bookings;"


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0001_initial"),
    ]

    operations = [
        # Required for the EXCLUDE constraint below: btree_gist supplies a
        # GiST operator class for resource_id (a scalar) so `= ` can sit
        # alongside `time_range WITH &&` in one constraint (Spec v1.0 §3;
        # confirmed available in Spike S1.1).
        BtreeGistExtension(),
        migrations.RunSQL(sql=ADD_CONSTRAINT_SQL, reverse_sql=DROP_CONSTRAINT_SQL),
    ]
