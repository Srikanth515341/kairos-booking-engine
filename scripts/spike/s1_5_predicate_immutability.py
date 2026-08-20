"""
S1.5 — Confirm a constraint predicate cannot reference now(). DESIGN-SHAPING
(RFC v1.0 §16, §10.4).

RFC §10.4's entire dual-reclamation design (reaper + cleanup-on-write, built
in Phase 17) exists ONLY because this is expected to fail. If it succeeds,
a hold's expiry could be expressed directly in the constraint and the reaper
becomes unnecessary.
"""

from common import DSN
import psycopg


def main() -> None:
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")
        cur.execute("DROP TABLE IF EXISTS spike_now_test;")
        cur.execute(
            """
            CREATE TABLE spike_now_test (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                resource_id UUID NOT NULL,
                time_range TSTZRANGE NOT NULL,
                status TEXT NOT NULL DEFAULT 'held',
                expires_at TIMESTAMPTZ
            );
            """
        )
        try:
            # This MUST fail: now() is not IMMUTABLE, and Postgres requires
            # index (and therefore exclusion constraint) predicates to be.
            cur.execute(
                """
                ALTER TABLE spike_now_test ADD CONSTRAINT test_now
                    EXCLUDE USING gist (resource_id WITH =, time_range WITH &&)
                    WHERE (status = 'held' AND expires_at > now());
                """
            )
            print("UNEXPECTED: the now()-dependent predicate was ACCEPTED.")
            print("This means RFC §10.4's dual-reclamation design is unnecessary —")
            print("expiry could be expressed directly in the constraint. Flag loudly.")
        except psycopg.Error as e:
            print("EXPECTED: the now()-dependent predicate was REJECTED.")
            print(f"SQLSTATE: {getattr(e, 'sqlstate', None)}")
            print(f"Error message: {e}")
            print()
            print("Consequence: RFC §10.4's dual mechanism (reaper + cleanup-on-write)")
            print("is confirmed necessary. Phase 17 proceeds as designed.")


if __name__ == "__main__":
    main()
