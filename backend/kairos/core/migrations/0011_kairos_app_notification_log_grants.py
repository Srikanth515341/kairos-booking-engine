"""Extends `kairos_app`'s grants (Phase 8/9/11/13/14/16) to
`notification_log` (Phase 18). Full DML except DELETE — matching
`waitlist_entry`/`waitlist_offer`'s grant (0008/0009) rather than
`audit_log`/`system_check_run`'s append-only one (0007): a notification's
row transitions `status` in place (pending -> sent/failed) as delivery is
retried, the same ordinary-application-table shape those two tables have.

No audit trigger attached, unlike waitlist_entry/waitlist_offer — this
table is itself a delivery-outcome LOG (the same category as
`system_check_run`, which also carries no audit trigger), not one of the
five entities Spec v1.0 §3 / RFC v1.0 §12 name as audited business state.
"""

from __future__ import annotations

from django.db import migrations

GRANTS_SQL = """
GRANT SELECT, INSERT, UPDATE ON notification_log TO kairos_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kairos_app;
"""

REVERSE_GRANTS_SQL = """
REVOKE ALL ON notification_log FROM kairos_app;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0010_notification_log"),
    ]

    operations = [
        migrations.RunSQL(sql=GRANTS_SQL, reverse_sql=REVERSE_GRANTS_SQL),
    ]
