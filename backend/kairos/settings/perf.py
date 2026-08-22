"""Settings for running `manage.py runserver` under Phase 29's load-testing
scripts (`scripts/perf/`). Otherwise identical to `dev.py` — real OIDC mock
issuer, console email — with exactly one deliberate difference.

`RATE_LIMIT_ENABLED = False`: `kairos.core.rate_limit`'s per-IP token
bucket (Phase 22) is a defense-in-depth fairness policy against a single
source hammering the API from one network location — which is exactly
what a synthetic load-testing harness running entirely from one machine
looks like, and is NOT what PRD A1's real traffic (many geographically
distinct users) would produce. Leaving it enabled here would make PERF-01
and CONC-06 measure "how fast does the per-IP limiter reject requests"
instead of "how fast does the write path complete," the same reasoning
`kairos.settings.test` already documents for disabling it during IDEM-06/
WL-01/WL-02's own rapid-fire test traffic. The per-PRINCIPAL limiter isn't
the concern — each simulated user in these scripts only writes at a
realistic individual rate — so this is scoped to the one limiter that's a
genuine measurement artifact here, not a blanket "disable everything."
"""

from .dev import *  # noqa: F403

RATE_LIMIT_ENABLED = False

# A load-testing session mints a batch of session tokens once at startup
# (kairos.identity.oidc.issue_session_token, called directly — not re-
# authenticated per request, since minting is setup overhead this project
# doesn't want polluting a write-latency measurement) and reuses them
# across a run that can span several minutes across PERF-01/02/03/CONC-06.
# The base 900s (15 min) default is generous for ordinary use but tight
# for a long escalation run — widened here, not globally, since this is a
# load-testing-specific concern, not a security posture change for dev/prod.
KAIROS_SESSION_TOKEN_TTL_SECONDS = 3600
