"""PRD FR46 — nullable, so every resource created before Phase 9 (and every
resource that never opts in) stays open to all; visibility for a restricted
resource is enforced in the view layer via AuthorizationService, not by a
DB-level policy.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0004_usergroup_usergroupmembership"),
        ("resources", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="resource",
            name="restricted_group",
            field=models.ForeignKey(
                blank=True,
                db_column="restricted_group_id",
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="restricted_resources",
                to="identity.usergroup",
            ),
        ),
    ]
