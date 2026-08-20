"""Spec v1.0 §3's `idx_series_materialized_through` — supports the rolling
materialization job (PRD FR14c, Phase 13). Raw SQL, not `models.Index`,
because the name is 32 characters — past Django's 30-character portability
limit (models.E034) for a `models.Index`, which exists for cross-database
compatibility this Postgres-only project doesn't need. Postgres itself
allows identifiers up to 63 characters, and the name is kept verbatim
rather than shortened to match the source document exactly.
"""

from __future__ import annotations

from django.db import migrations

CREATE_INDEX_SQL = """
CREATE INDEX idx_series_materialized_through ON recurring_series (materialized_through)
    WHERE status = 'active';
"""

DROP_INDEX_SQL = "DROP INDEX IF EXISTS idx_series_materialized_through;"


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0003_recurring_series"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_INDEX_SQL, reverse_sql=DROP_INDEX_SQL),
    ]
