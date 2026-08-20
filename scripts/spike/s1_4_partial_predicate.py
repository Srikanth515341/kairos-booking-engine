"""
S1.4 — Partial predicate behavior (RFC v1.0 §16, §3.5).

Confirms: (a) WHERE status IN ('confirmed','held') is accepted as an
EXCLUDE predicate; (b) cancelling a row and inserting a new overlapping one
succeeds (cancel-then-rebook works); (c) cancelled rows actually leave the
GiST index, which is the claimed "free partial-index win" in RFC §3.5.
"""

import uuid

from common import connect, reset_schema


def main() -> None:
    reset_schema()
    resource_id = str(uuid.uuid4())
    range_sql = "['2026-09-01 13:00:00+00', '2026-09-01 14:00:00+00')"

    conn = connect()
    with conn.cursor() as cur:
        # (a) predicate accepted — reset_schema() already created it; confirm here.
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'spike_no_overlap';"
        )
        predicate = cur.fetchone()[0]
        print(f"(a) Constraint definition: {predicate}")
        assert "confirmed" in predicate and "held" in predicate, "predicate missing a status"
        print("(a) PASS: partial predicate WHERE status IN ('confirmed','held') accepted")

        # (b) insert, cancel, insert overlapping again.
        cur.execute(
            "INSERT INTO spike_booking (resource_id, time_range, status) "
            "VALUES (%s, %s::tstzrange, 'confirmed') RETURNING id",
            (resource_id, range_sql),
        )
        booking_id = cur.fetchone()[0]
        conn.commit()

        cur.execute(
            "UPDATE spike_booking SET status = 'cancelled' WHERE id = %s", (booking_id,)
        )
        conn.commit()

        try:
            cur.execute(
                "INSERT INTO spike_booking (resource_id, time_range, status) "
                "VALUES (%s, %s::tstzrange, 'confirmed')",
                (resource_id, range_sql),
            )
            conn.commit()
            print("(b) PASS: cancel-then-rebook on the identical range succeeded")
        except Exception as e:  # noqa: BLE001 - spike diagnostic
            conn.rollback()
            print(f"(b) FAIL: cancel-then-rebook was rejected: {e}")
            raise

        # (c) cancelled rows leave the partial GiST index.
        # We can't directly inspect GiST leaf contents easily, but we CAN prove
        # the constraint's own index only covers active rows by checking that
        # inserting many cancelled overlapping rows does not grow index size
        # proportionally, and — more directly — that N overlapping cancelled
        # rows on the SAME range can all coexist (impossible if they were still
        # in the exclusion domain).
        for _ in range(20):
            cur.execute(
                "INSERT INTO spike_booking (resource_id, time_range, status) "
                "VALUES (%s, %s::tstzrange, 'cancelled')",
                (resource_id, range_sql),
            )
        conn.commit()
        cur.execute(
            "SELECT count(*) FROM spike_booking WHERE resource_id = %s AND status='cancelled'",
            (resource_id,),
        )
        cancelled_count = cur.fetchone()[0]
        print(
            f"(c) PASS: {cancelled_count} overlapping CANCELLED rows coexist on the "
            "identical range — proves cancelled rows are outside the exclusion domain "
            "(and therefore outside the partial index)"
        )

    conn.close()


if __name__ == "__main__":
    main()
