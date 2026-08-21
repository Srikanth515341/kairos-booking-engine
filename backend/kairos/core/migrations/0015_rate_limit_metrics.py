"""Implementation Plan Phase 22; RFC v1.0 §8.2 — extends `request_metric`
(Phase 21) for rate limiting: a `rate_limited` metric_type (429s get their
own type, checked first in classify_metric_type alongside auth_failure)
and a `principal_id` column (the authenticated AppUser.id as text,
populated on every row — Rollout v1.0 §6.2's "per-principal breakdown"
needs it specifically on rate-limited rows, but recording it universally
costs nothing extra). Constraint dropped and re-added rather than altered
in place — Django's own generated shape for a CheckConstraint whose
condition changes.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_kairos_app_phase21_grants"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="requestmetric",
            name="request_metric_type_check",
        ),
        migrations.AddField(
            model_name="requestmetric",
            name="principal_id",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="requestmetric",
            name="metric_type",
            field=models.TextField(
                choices=[
                    ("booking_write", "Booking write"),
                    ("availability_read", "Availability read"),
                    ("auth_failure", "Auth failure"),
                    ("rate_limited", "Rate limited"),
                    ("other", "Other"),
                ]
            ),
        ),
        migrations.AddIndex(
            model_name="requestmetric",
            index=models.Index(
                fields=["principal_id", "-recorded_at"], name="idx_request_metric_principal"
            ),
        ),
        migrations.AddConstraint(
            model_name="requestmetric",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "metric_type__in",
                        [
                            "booking_write",
                            "availability_read",
                            "auth_failure",
                            "rate_limited",
                            "other",
                        ],
                    )
                ),
                name="request_metric_type_check",
            ),
        ),
    ]
