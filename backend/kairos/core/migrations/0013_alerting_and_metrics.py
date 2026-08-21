# Generated for Implementation Plan Phase 21 (Rollout v1.0 §6.1, §6.2).

import uuid

import django.core.serializers.json
import django.db.models.functions.datetime
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0012_notification_log_series_owner_deactivated_type"),
    ]

    operations = [
        migrations.CreateModel(
            name="AlertEvent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "alert_key",
                    models.TextField(
                        choices=[
                            ("schema_assertion", "Schema assertion"),
                            ("reconciliation", "Reconciliation"),
                            ("hold_reaper", "Hold reaper"),
                            ("offer_cascade", "Offer cascade"),
                            ("series_materialization", "Series materialization"),
                            ("tzdata_rematerialization", "Tzdata re-materialization"),
                            ("audit_actor_unknown", "Audit actor unknown"),
                        ]
                    ),
                ),
                (
                    "severity",
                    models.TextField(
                        choices=[("sev_1", "SEV-1"), ("sev_2", "SEV-2"), ("sev_3", "SEV-3")]
                    ),
                ),
                ("message", models.TextField()),
                (
                    "context",
                    models.JSONField(
                        default=dict, encoder=django.core.serializers.json.DjangoJSONEncoder
                    ),
                ),
                (
                    "fired_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now()),
                ),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                (
                    "email_status",
                    models.TextField(
                        choices=[
                            ("pending", "Pending"),
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                    ),
                ),
                ("email_attempts", models.IntegerField(default=0)),
                ("email_last_error", models.TextField(blank=True, null=True)),
                ("email_sent_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "alert_event",
                "indexes": [
                    models.Index(fields=["alert_key", "-fired_at"], name="idx_alert_event_key")
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "alert_key__in",
                                [
                                    "schema_assertion",
                                    "reconciliation",
                                    "hold_reaper",
                                    "offer_cascade",
                                    "series_materialization",
                                    "tzdata_rematerialization",
                                    "audit_actor_unknown",
                                ],
                            )
                        ),
                        name="alert_event_key_check",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("severity__in", ["sev_1", "sev_2", "sev_3"])),
                        name="alert_event_severity_check",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("resolved_at__isnull", True)),
                        fields=("alert_key",),
                        name="uniq_open_alert_per_key",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="RequestMetric",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "metric_type",
                    models.TextField(
                        choices=[
                            ("booking_write", "Booking write"),
                            ("availability_read", "Availability read"),
                            ("auth_failure", "Auth failure"),
                            ("other", "Other"),
                        ]
                    ),
                ),
                ("method", models.TextField()),
                ("path", models.TextField()),
                ("status_code", models.IntegerField()),
                ("duration_ms", models.IntegerField(blank=True, null=True)),
                ("cause", models.TextField(blank=True, null=True)),
                (
                    "recorded_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now()),
                ),
            ],
            options={
                "db_table": "request_metric",
                "indexes": [
                    models.Index(
                        fields=["metric_type", "-recorded_at"], name="idx_request_metric_type"
                    ),
                    models.Index(
                        fields=["status_code", "-recorded_at"], name="idx_request_metric_status"
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "metric_type__in",
                                ["booking_write", "availability_read", "auth_failure", "other"],
                            )
                        ),
                        name="request_metric_type_check",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="OperationalHeartbeat",
            fields=[
                ("name", models.TextField(primary_key=True, serialize=False)),
                ("last_run_at", models.DateTimeField()),
                (
                    "findings",
                    models.JSONField(
                        default=dict, encoder=django.core.serializers.json.DjangoJSONEncoder
                    ),
                ),
            ],
            options={
                "db_table": "operational_heartbeat",
            },
        ),
    ]
