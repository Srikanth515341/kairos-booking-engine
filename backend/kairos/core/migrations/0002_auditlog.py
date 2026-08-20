"""AuditLog (Spec v1.0 §3; PRD FR39–FR43) — the table, not yet the trigger
that writes to it. The trigger function, the triggers themselves, and the
kairos_app role/grants that make this table append-only in practice are
all raw SQL in 0003_audit_trail_triggers_and_grants, since none of that is
expressible through Django's model layer. This migration only needs to get
the table, its CHECK constraint, and its three indexes onto disk, which
Django's ORM handles natively — the same split Phase 2 used (ordinary
CreateModel for the table, RunSQL only for the EXCLUDE constraint).
"""

from __future__ import annotations

from django.db import migrations, models
from django.db.models.functions import Now


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("entity_type", models.TextField()),
                ("entity_id", models.UUIDField()),
                ("action", models.TextField()),
                ("actor_id", models.UUIDField(blank=True, null=True)),
                (
                    "actor_type",
                    models.TextField(
                        choices=[
                            ("user", "User"),
                            ("admin", "Admin"),
                            ("system", "System"),
                            ("unknown", "Unknown"),
                        ],
                        default="unknown",
                    ),
                ),
                ("reason", models.TextField(blank=True, null=True)),
                ("request_id", models.TextField(blank=True, null=True)),
                ("before_state", models.JSONField(blank=True, null=True)),
                ("after_state", models.JSONField(blank=True, null=True)),
                (
                    "occurred_at",
                    models.DateTimeField(db_default=Now()),
                ),
            ],
            options={
                "db_table": "audit_log",
                "indexes": [
                    models.Index(
                        fields=["entity_type", "entity_id", "-occurred_at"],
                        name="idx_audit_entity",
                    ),
                    models.Index(fields=["actor_id", "-occurred_at"], name="idx_audit_actor"),
                    models.Index(fields=["request_id"], name="idx_audit_request"),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("actor_type__in", ["user", "admin", "system", "unknown"])
                        ),
                        name="audit_log_actor_type_check",
                    ),
                ],
            },
        ),
    ]
