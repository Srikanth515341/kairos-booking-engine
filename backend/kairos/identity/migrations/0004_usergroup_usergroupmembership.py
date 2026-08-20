"""PRD FR46 / RFC v1.0 §8.2's "user group" concept — never defined in
Spec v1.0 §3, which predates Phase 9 (see kairos.identity.models.UserGroup
for the full rationale). `Resource.restricted_group` (next migration) is
the only thing that references these; no existing resource is affected by
their mere existence.
"""

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0003_alter_resourceadmin_id"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserGroup",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("name", models.TextField(unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "user_group",
            },
        ),
        migrations.CreateModel(
            name="UserGroupMembership",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "group",
                    models.ForeignKey(
                        db_column="group_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memberships",
                        to="identity.usergroup",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        db_column="user_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="group_memberships",
                        to="identity.appuser",
                    ),
                ),
            ],
            options={
                "db_table": "user_group_membership",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("group", "user"), name="user_group_membership_unique"
                    )
                ],
            },
        ),
    ]
