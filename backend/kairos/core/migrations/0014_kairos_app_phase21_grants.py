"""Extends `kairos_app`'s grants (Phase 8/9/11/13/14/16/18) to the three
Phase 21 tables. GRANT statements aren't retroactive — the same "a new
migration, not an edit to an earlier one" pattern this project has used
for every prior grants extension.

`alert_event`: full DML except DELETE — like `notification_log`/
`waitlist_entry`/`waitlist_offer`, a row's `email_status`/`resolved_at`
are revised in place as delivery/resolution happen, not append-only like
`audit_log`/`system_check_run`.

`request_metric`: SELECT/INSERT/DELETE, no UPDATE — one row is written
once per request and never revised, but `kairos.core.metrics.
prune_old_request_metrics` needs DELETE (unlike every other operational
log table in this project, which is either append-only forever or
revised-in-place-never-deleted).

`operational_heartbeat`: SELECT/INSERT/UPDATE — a genuine upsert-in-place
table (`update_or_create` keyed on `name`), the same shape as
`notification_log`.

No audit trigger on any of the three — all three are operational/
observability tables, the same category `system_check_run`/
`notification_log` already established as carrying none, not one of the
five entities Spec v1.0 §3 / RFC v1.0 §12 name as audited business state.
"""

from __future__ import annotations

from django.db import migrations

GRANTS_SQL = """
GRANT SELECT, INSERT, UPDATE ON alert_event TO kairos_app;
GRANT SELECT, INSERT, DELETE ON request_metric TO kairos_app;
GRANT SELECT, INSERT, UPDATE ON operational_heartbeat TO kairos_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kairos_app;
"""

REVERSE_GRANTS_SQL = """
REVOKE ALL ON alert_event FROM kairos_app;
REVOKE ALL ON request_metric FROM kairos_app;
REVOKE ALL ON operational_heartbeat FROM kairos_app;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0013_alerting_and_metrics"),
    ]

    operations = [
        migrations.RunSQL(sql=GRANTS_SQL, reverse_sql=REVERSE_GRANTS_SQL),
    ]
