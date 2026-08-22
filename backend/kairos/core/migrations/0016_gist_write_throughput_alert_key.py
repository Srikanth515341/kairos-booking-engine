# Generated for Implementation Plan Phase 29 (Rollout v1.0 §6's own "GiST
# write throughput on booking" row — left with no real alert writer in
# Phase 21, deliberately: its threshold was "Set from CONC-06's real
# characterization data — deliberately not invented here"). CHECK
# constraints aren't retroactive to a widened TextChoices enum — the same
# RemoveConstraint/AlterField/AddConstraint shape migration 0012 already
# used for NotificationType.SERIES_OWNER_DEACTIVATED.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0015_rate_limit_metrics"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="alertevent",
            name="alert_event_key_check",
        ),
        migrations.AlterField(
            model_name="alertevent",
            name="alert_key",
            field=models.TextField(
                choices=[
                    ("schema_assertion", "Schema assertion"),
                    ("reconciliation", "Reconciliation"),
                    ("hold_reaper", "Hold reaper"),
                    ("offer_cascade", "Offer cascade"),
                    ("series_materialization", "Series materialization"),
                    ("tzdata_rematerialization", "Tzdata re-materialization"),
                    ("audit_actor_unknown", "Audit actor unknown"),
                    ("gist_write_throughput", "GiST write throughput"),
                ]
            ),
        ),
        migrations.AddConstraint(
            model_name="alertevent",
            constraint=models.CheckConstraint(
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
                            "gist_write_throughput",
                        ],
                    )
                ),
                name="alert_event_key_check",
            ),
        ),
    ]
