"""Test Plan TZ-03 Test B: "a periodic (not per-commit) job compares
installed tzdata against the latest IANA release and alerts — does not
fail a build — if behind." Deliberately separate from
`kairos.bookings.tasks.rematerialize_stale_series`, which compares the
CURRENTLY INSTALLED version against what's recorded per-series (internal
consistency) — this compares the INSTALLED version against the latest
release PUBLISHED UPSTREAM (external staleness). Spec v1.0 §3's
`system_check_run.check_name` CHECK constraint has no slot for this
concern (only `tzdata_rematerialization`, which means something
different), so this alerts via logging only — matching RFC v1.0 §14's own
scoping of full alerting infrastructure to Phase 21.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from urllib.error import URLError

from kairos.core.timezones import tzdata_version

logger = logging.getLogger(__name__)

PYPI_TZDATA_JSON_URL = "https://pypi.org/pypi/tzdata/json"


def fetch_latest_tzdata_version(timeout_seconds: float = 5.0) -> str | None:
    """The `tzdata` PyPI package's version tracks the IANA release
    identifier it packages (this project's own pin — Phase 10 — already
    relies on that correspondence), so PyPI's own release feed doubles as
    a "latest known IANA release" source without a dependency on IANA's
    own site structure. Unlike `kairos/identity/oidc.py`'s
    `_fetch_jwks_public_key` (Phase 9) or IDEM-07/08, this one is NOT just
    structurally complete — it was verified live during Phase 13 against
    the real `https://pypi.org/pypi/tzdata/json` endpoint, returning the
    correct current release ("2026.3", matching this project's own pin).
    The test suite still mocks it, for determinism and to exercise the
    behind/unreachable branches on demand, not because the live path is
    unproven. Returns None (never raises) on any network failure — a
    reachability problem must not crash a scheduled job, only prevent it
    from concluding anything this run.
    """
    try:
        with urllib.request.urlopen(PYPI_TZDATA_JSON_URL, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload["info"]["version"])
    except (URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("tzdata_drift_check_unreachable", extra={"error": str(exc)})
        return None


def check_tzdata_drift() -> dict[str, str | bool | None]:
    installed = tzdata_version()
    latest = fetch_latest_tzdata_version()

    if latest is None:
        return {"installed_version": installed, "latest_version": None, "behind": None}

    behind = latest != installed
    result: dict[str, str | bool | None] = {
        "installed_version": installed,
        "latest_version": latest,
        "behind": behind,
    }
    if behind:
        # Being behind is not necessarily broken yet, only at risk (Test
        # Plan TZ-03) — a WARNING, never an exception, and this never
        # fails a build (no CI job consults this function at all).
        logger.warning("tzdata_drift_detected", extra=result)
    else:
        logger.info("tzdata_drift_check_ok", extra=result)
    return result
