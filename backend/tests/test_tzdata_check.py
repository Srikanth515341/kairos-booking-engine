"""Test Plan TZ-03 Test B: "a periodic (not per-commit) job compares
installed tzdata against the latest IANA release and alerts — does not
fail a build — if behind." `fetch_latest_tzdata_version`'s live network
call is mocked here — structurally complete but genuinely untested against
the real PyPI endpoint, the same documented-gap pattern as
`kairos/identity/oidc.py`'s `_fetch_jwks_public_key` (Phase 9).
"""

from __future__ import annotations

import pytest

from kairos.core.timezones import tzdata_version
from kairos.core.tzdata_check import check_tzdata_drift


def test_check_tzdata_drift_reports_ok_when_current(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kairos.core.tzdata_check.fetch_latest_tzdata_version", lambda **_: tzdata_version()
    )
    result = check_tzdata_drift()
    assert result == {
        "installed_version": tzdata_version(),
        "latest_version": tzdata_version(),
        "behind": False,
    }


def test_check_tzdata_drift_alerts_without_raising_when_behind(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "kairos.core.tzdata_check.fetch_latest_tzdata_version", lambda **_: "2099.9"
    )
    with caplog.at_level("WARNING", logger="kairos.core.tzdata_check"):
        result = check_tzdata_drift()  # must not raise — "does not fail a build"

    assert result["behind"] is True
    assert result["latest_version"] == "2099.9"
    assert any(r.message == "tzdata_drift_detected" for r in caplog.records)


def test_check_tzdata_drift_handles_an_unreachable_endpoint_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kairos.core.tzdata_check.fetch_latest_tzdata_version", lambda **_: None)
    result = check_tzdata_drift()  # must not raise
    assert result == {
        "installed_version": tzdata_version(),
        "latest_version": None,
        "behind": None,
    }
