import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "kairos.core"
    label = "core"

    def ready(self) -> None:
        # Implementation Plan Phase 10 / Test Plan TZ-03 Test A: staleness
        # must be a visible, tracked quantity, not silent — logging the
        # pinned tzdata version on every startup is the "on deploy" half of
        # that; the CI-form pin assertion (tests/test_timezones.py) is the
        # other.
        from kairos.core.timezones import tzdata_version

        logger.info("tzdata_version_at_startup", extra={"tzdata_version": tzdata_version()})
