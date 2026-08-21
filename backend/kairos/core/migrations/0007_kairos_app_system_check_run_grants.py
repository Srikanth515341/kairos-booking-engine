"""Extends `kairos_app`'s grants (Phase 8/9/11) to `system_check_run`
(Phase 13). GRANT statements aren't retroactive — see 0004/0005's
identical rationale. `UPDATE`/`DELETE` are deliberately withheld, matching
`audit_log`'s append-only philosophy: a check run is a historical record
of what a job observed at run time, not a row anything should ever revise
after the fact — the SAME reasoning, applied to a different table.
"""

from __future__ import annotations

from django.db import migrations

GRANTS_SQL = """
GRANT SELECT, INSERT ON system_check_run TO kairos_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kairos_app;
"""

REVERSE_GRANTS_SQL = """
REVOKE ALL ON system_check_run FROM kairos_app;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0006_system_check_run"),
    ]

    operations = [
        migrations.RunSQL(sql=GRANTS_SQL, reverse_sql=REVERSE_GRANTS_SQL),
    ]
