"""Extends `kairos_app`'s grants (Phase 8/9/11/13) to `waitlist_entry`
(Phase 14) and attaches the audit trigger 0003's `write_audit_log()`
already anticipated for it — that migration's own comment says "waitlist_
entry and waitlist_offer triggers are added in Phases 14/16, when those
tables exist," and this is that promise kept, not a new mechanism.

Unlike `audit_log`/`system_check_run`'s deliberately append-only grant
(0007), `waitlist_entry` gets full DML — a live entry transitions status
in place (join → cancel today; join → offered → fulfilled/expired from
Phase 16 on), the same ordinary-application-table shape `booking` and
`recurring_series` already have.
"""

from __future__ import annotations

from django.db import migrations

GRANTS_SQL = """
GRANT SELECT, INSERT, UPDATE, DELETE ON waitlist_entry TO kairos_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kairos_app;
"""

REVERSE_GRANTS_SQL = """
REVOKE ALL ON waitlist_entry FROM kairos_app;
"""

TRIGGER_SQL = """
CREATE TRIGGER audit_waitlist_entry AFTER INSERT OR UPDATE OR DELETE ON waitlist_entry
    FOR EACH ROW EXECUTE FUNCTION write_audit_log();
"""

REVERSE_TRIGGER_SQL = "DROP TRIGGER IF EXISTS audit_waitlist_entry ON waitlist_entry;"


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0007_kairos_app_system_check_run_grants"),
        ("waitlist", "0002_uniq_live_waitlist_per_user_slot"),
    ]

    operations = [
        migrations.RunSQL(sql=GRANTS_SQL, reverse_sql=REVERSE_GRANTS_SQL),
        migrations.RunSQL(sql=TRIGGER_SQL, reverse_sql=REVERSE_TRIGGER_SQL),
    ]
