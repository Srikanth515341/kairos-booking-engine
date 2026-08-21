"""Rate limiting (Implementation Plan Phase 22; RFC v1.0 §8.2).

This is a FAIRNESS policy, not a correctness guarantee — and that
distinction is not a throwaway comment, it's the reason for every choice
below. The exclusion constraint (`no_overlapping_bookings`) is enforced at
the schema level precisely because NO code path, present or future, may
ever bypass it: a single double-booking is the one outcome this entire
project exists to make structurally impossible. A rate limiter has no
equivalent stakes. If Redis is briefly unavailable, or two application
processes each let one extra request through during a race in the token
bucket's own read-modify-write, the consequence is "one user got a couple
of extra requests" — mildly unfair, never unsafe, never a double booking.
Conflating the two — e.g. by making booking creation FAIL when the rate
limiter can't be consulted — would be a design error: it would let an
infrastructure hiccup in a fairness mechanism block the actual product.
So this module fails OPEN (allows the request) whenever Redis can't be
reached, exactly the same "Redis is a LIVENESS dependency, never a
correctness one" principle RFC v1.0 §4.3 already established for Celery
(kairos.waitlist.tasks.dispatch_cascade's identical broad-except-and-
degrade shape).

Storage is Redis, not Postgres: a token bucket is read-modified-written on
every single request to a hot endpoint, and Redis's own Lua-script
atomicity gives correct-enough concurrent updates for a fairness policy
without adding write load to the database the exclusion constraint
actually depends on. `redis-py` is already an installed dependency
(`celery[redis]`, pyproject.toml, first used directly by `kairos.core.
metrics.redis_availability` in Phase 21) — no new package.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from django.conf import settings
from rest_framework.throttling import BaseThrottle

from kairos.core.constants import (
    RATE_LIMIT_BOOKING_CREATE_CAPACITY,
    RATE_LIMIT_BOOKING_CREATE_WINDOW_SECONDS,
    RATE_LIMIT_PER_IP_CAPACITY,
    RATE_LIMIT_PER_IP_WINDOW_SECONDS,
)

logger = logging.getLogger(__name__)

# A standard token-bucket, implemented as a Lua script so the read-refill-
# check-decrement-write sequence is atomic within Redis itself (Redis
# executes a single script as one uninterruptible operation) — without
# this, two concurrent requests from the same principal could both read
# "1 token left" and both be allowed, which is exactly the kind of
# looseness this module's own docstring says is tolerable for a fairness
# policy but not one this implementation invites for free when avoiding
# it costs nothing. `now` is passed in as an ARGV, not read via Redis's
# own TIME command, so the bucket's behavior is fully deterministic and
# testable from Python (kairos.core.rate_limit.TokenBucket.check(now=...)),
# the same "inject `now`, default to real time" convention this project
# uses everywhere else (heartbeat_is_stale, evaluate_alerts, ...).
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local ttl_seconds = tonumber(ARGV[5])

local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(bucket[1])
local last_refill = tonumber(bucket[2])

if tokens == nil then
    tokens = capacity
    last_refill = now
end

local elapsed = now - last_refill
if elapsed < 0 then
    elapsed = 0
end
tokens = math.min(capacity, tokens + elapsed * refill_rate)

local allowed = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
redis.call('EXPIRE', key, ttl_seconds)

local retry_after = 0
if allowed == 0 and refill_rate > 0 then
    retry_after = math.ceil((requested - tokens) / refill_rate)
end

return {allowed, tostring(tokens), retry_after}
"""


class TokenBucket:
    """A single named token bucket, backed by Redis. `check()` is the only
    method that matters: it atomically decides whether one more request is
    allowed and, if not, how long to wait. Fails OPEN — see module
    docstring — on ANY Redis error, deliberately broad for the identical
    reason `kairos.waitlist.tasks.dispatch_cascade` catches broadly: this
    is a fire-and-forget-shaped check where enumerating every possible
    connection-layer exception risks missing one and silently turning a
    Redis blip into blocked bookings, the opposite of what this module
    exists to prevent.
    """

    def __init__(self, *, capacity: int, window_seconds: int) -> None:
        self.capacity = capacity
        self.refill_rate = capacity / window_seconds
        # Bucket state is meaningless once it would have fully refilled
        # anyway — EXPIRE at that point instead of forever, so an inactive
        # principal's key doesn't linger in Redis indefinitely.
        self._ttl_seconds = window_seconds * 2

    def check(self, key: str, *, now: float | None = None, requested: int = 1) -> tuple[bool, int]:
        """Returns `(allowed, retry_after_seconds)`. `now` defaults to
        `time.time()`; pass it explicitly only from tests that need
        deterministic control (mirrors `heartbeat_is_stale(now=...)`).
        """
        now = now if now is not None else time.time()
        try:
            client = _redis_client()
            result: Any = client.eval(
                _TOKEN_BUCKET_LUA,
                1,
                key,
                self.capacity,
                self.refill_rate,
                now,
                requested,
                self._ttl_seconds,
            )
            allowed, _tokens_remaining, retry_after = result
            return bool(int(allowed)), int(retry_after)
        except Exception:
            logger.warning(
                "rate_limit_check_failed_failing_open", extra={"key": key}, exc_info=True
            )
            return True, 0


_redis_client_singleton: Any = None


def _redis_client() -> Any:
    # Deferred import (redis-py, not a hard Django dependency conceptually
    # even though it's always installed transitively) + lazily
    # constructed singleton, mirroring kairos.core.metrics.
    # redis_availability's own "import redis inside the function" choice.
    global _redis_client_singleton
    if _redis_client_singleton is None:
        import redis

        _redis_client_singleton = redis.from_url(  # type: ignore[no-untyped-call]
            settings.CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1
        )
    return _redis_client_singleton


def client_ip(request: Any) -> str:
    """Plain `REMOTE_ADDR` — the actual TCP-level source address, not
    `X-Forwarded-For`. This app doesn't run behind a documented trusted
    reverse proxy in this stack (`manage.py runserver` directly, per
    README) — trusting a client-supplied header for anything
    security-relevant without a proxy that's known to strip/overwrite it
    would let a client simply lie about its own IP and evade this exact
    limiter. A real deployment that DOES terminate behind a trusted
    gateway should set `REMOTE_ADDR` correctly at that layer (e.g. via the
    gateway's own proxy_set_header / X-Forwarded-For handling into WSGI's
    REMOTE_ADDR), not have this application trust an arbitrary header.
    """
    return str(request.META.get("REMOTE_ADDR", "") or "")


class BookingCreatePrincipalThrottle(BaseThrottle):
    """Per-principal token bucket on booking creation ONLY (RFC v1.0
    §8.2's own scope — not every mutating endpoint). `kairos.bookings.
    views.BookingCollectionView` handles both POST (create) and GET
    (list); this throttle explicitly no-ops for anything but POST rather
    than the view splitting into two classes just to attach throttling
    to one verb.
    """

    _bucket = TokenBucket(
        capacity=RATE_LIMIT_BOOKING_CREATE_CAPACITY,
        window_seconds=RATE_LIMIT_BOOKING_CREATE_WINDOW_SECONDS,
    )

    def allow_request(self, request: Any, view: Any) -> bool:
        if not settings.RATE_LIMIT_ENABLED or request.method != "POST":
            return True
        user = getattr(request, "user", None)
        principal_id = str(user.id) if user is not None else None
        if principal_id is None:
            # IsAuthenticated already gates this view before throttling
            # runs (DRF's dispatch order: authenticate -> permissions ->
            # throttles), so this is unreachable in practice — kept as an
            # explicit, safe default rather than an assumption, in case
            # this throttle is ever attached to a view without that
            # permission class.
            return True

        allowed, retry_after = self._bucket.check(f"ratelimit:booking_create:{principal_id}")
        if not allowed:
            request._kairos_throttle_cause = "per_principal_token_bucket"
            self._wait = retry_after
            return False
        return True

    def wait(self) -> int | None:
        return getattr(self, "_wait", None)


class PerIPThrottle(BaseThrottle):
    """Coarser, per-IP token bucket — defense-in-depth, explicitly NOT a
    solution to distributed abuse (this phase's own Scope IN wording): a
    determined abuser with many source IPs sails through this unaffected,
    which is expected, not a gap this class is meant to close. The real
    defense against that is a gateway/CDN-level control this codebase
    doesn't own — see `client_ip`'s own docstring for why this doesn't
    trust `X-Forwarded-For` in the absence of one.
    """

    _bucket = TokenBucket(
        capacity=RATE_LIMIT_PER_IP_CAPACITY, window_seconds=RATE_LIMIT_PER_IP_WINDOW_SECONDS
    )

    def allow_request(self, request: Any, view: Any) -> bool:
        if not settings.RATE_LIMIT_ENABLED or request.method != "POST":
            return True
        ip = client_ip(request)
        if not ip:
            return True

        allowed, retry_after = self._bucket.check(f"ratelimit:per_ip:{ip}")
        if not allowed:
            request._kairos_throttle_cause = "per_ip_token_bucket"
            self._wait = retry_after
            return False
        return True

    def wait(self) -> int | None:
        return getattr(self, "_wait", None)
