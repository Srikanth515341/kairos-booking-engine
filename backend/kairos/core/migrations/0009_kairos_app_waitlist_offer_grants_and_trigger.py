"""Extends `kairos_app`'s grants (Phase 8/9/11/13/14) to `waitlist_offer`
(Phase 16) and attaches the audit trigger 0003's own comment already
promised: "waitlist_entry and waitlist_offer triggers are added in
Phases 14/16, when those tables exist" — this is that promise kept.

Full DML, matching `waitlist_entry`'s grant (0008) rather than
`audit_log`/`system_check_run`'s append-only one (0007): an offer
transitions status in place (active → confirmed/expired/declined) over
its lifetime, the same ordinary-application-table shape as every other
table in this schema except the two genuinely append-only ones.
"""

from __future__ import annotations

from django.db import migrations

GRANTS_SQL = """
GRANT SELECT, INSERT, UPDATE, DELETE ON waitlist_offer TO kairos_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kairos_app;
"""

REVERSE_GRANTS_SQL = """
REVOKE ALL ON waitlist_offer FROM kairos_app;
"""

TRIGGER_SQL = """
CREATE TRIGGER audit_waitlist_offer AFTER INSERT OR UPDATE OR DELETE ON waitlist_offer
    FOR EACH ROW EXECUTE FUNCTION write_audit_log();
"""

REVERSE_TRIGGER_SQL = "DROP TRIGGER IF EXISTS audit_waitlist_offer ON waitlist_offer;"


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_kairos_app_waitlist_entry_grants_and_trigger"),
        ("waitlist", "0003_waitlistoffer"),
    ]

    operations = [
        migrations.RunSQL(sql=GRANTS_SQL, reverse_sql=REVERSE_GRANTS_SQL),
        migrations.RunSQL(sql=TRIGGER_SQL, reverse_sql=REVERSE_TRIGGER_SQL),
    ]
