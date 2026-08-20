"""Extends `kairos_app`'s grants (Phase 8, core/migrations/0003; Phase 9,
core/migrations/0004) to `recurring_series` (Phase 11). GRANT statements
aren't retroactive — a table created after Phase 8's "every table that
exists at the time" grant isn't covered by it, so every new table needs
its own follow-up grant migration or the running application (which
connects as `kairos_app`, never the superuser — Phase 8) will get
`permission denied` the first time anything reads or writes it, exactly
like the `django_migrations` gap Phase 8 found live.
"""

from __future__ import annotations

from django.db import migrations

GRANTS_SQL = """
GRANT SELECT, INSERT, UPDATE, DELETE ON recurring_series TO kairos_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kairos_app;
"""

REVERSE_GRANTS_SQL = """
REVOKE ALL ON recurring_series FROM kairos_app;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_kairos_app_group_grants"),
        ("bookings", "0003_recurring_series"),
    ]

    operations = [
        migrations.RunSQL(sql=GRANTS_SQL, reverse_sql=REVERSE_GRANTS_SQL),
    ]
