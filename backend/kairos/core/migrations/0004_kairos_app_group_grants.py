"""Extends `kairos_app`'s grants (Phase 8, core/migrations/0003) to the two
tables Phase 9's restricted-resources feature adds — `user_group` and
`user_group_membership`. Phase 8's own migration is already merged to
`main`, so this is a NEW migration rather than an edit to that one; the
"every sequence in the schema" grant it already issued doesn't
automatically cover a table created afterward — GRANT statements aren't
retroactive, they apply to what exists at the moment they run.
"""

from __future__ import annotations

from django.db import migrations

GRANTS_SQL = """
GRANT SELECT, INSERT, UPDATE, DELETE ON user_group, user_group_membership TO kairos_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kairos_app;
"""

REVERSE_GRANTS_SQL = """
REVOKE ALL ON user_group, user_group_membership FROM kairos_app;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_audit_trail_triggers_and_grants"),
        ("identity", "0004_usergroup_usergroupmembership"),
    ]

    operations = [
        migrations.RunSQL(sql=GRANTS_SQL, reverse_sql=REVERSE_GRANTS_SQL),
    ]
