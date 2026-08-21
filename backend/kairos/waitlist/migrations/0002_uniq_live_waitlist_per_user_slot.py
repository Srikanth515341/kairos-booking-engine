"""Spec v1.0 §3's `uniq_live_waitlist_per_user_slot`. Raw SQL, not
`UniqueConstraint`, because the name is 32 characters — past Django's
30-character portability limit (models.E033), the identical situation
Phase 11's `idx_series_materialized_through`
(bookings/0004_series_materialized_through_index.py) already established
the precedent for. Postgres itself allows identifiers up to 63 characters;
the name is kept verbatim rather than shortened to match the source
document exactly.

This is the load-bearing comment site for SEC-03(b)/(c) (Implementation
Plan §1.3's "the containment eligibility rule" site is the @> query in
kairos.waitlist.services — this index is a companion guarantee, not that
one, but the same "STOP before weakening this" discipline applies): the
partial index covers BOTH 'waiting' and 'offered' so a user cannot re-join
while already holding an outstanding offer (RFC v1.0 §8.2). Narrowing the
WHERE clause to 'waiting' alone would silently let a user with a live offer
open a second entry for the identical range.
"""

from __future__ import annotations

from django.db import migrations

CREATE_INDEX_SQL = """
CREATE UNIQUE INDEX uniq_live_waitlist_per_user_slot
    ON waitlist_entry (user_id, resource_id, time_range)
    WHERE status IN ('waiting', 'offered');
"""

DROP_INDEX_SQL = "DROP INDEX IF EXISTS uniq_live_waitlist_per_user_slot;"


class Migration(migrations.Migration):
    dependencies = [
        ("waitlist", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_INDEX_SQL, reverse_sql=DROP_INDEX_SQL),
    ]
