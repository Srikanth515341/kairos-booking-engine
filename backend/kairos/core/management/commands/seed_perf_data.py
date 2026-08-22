"""Seeds a PRD A1-scale dataset for Phase 29's performance/load testing
(Test Plan v1.0 §11: "numbers from an empty database are close to
meaningless — index behavior, planning, and pool contention all depend on
realistic size"). NOT a fixture for pytest (those stay minimal and
per-test, by design) and NOT the application's own data model changing in
any way — this only populates rows through the real ORM, the same tables
every other write path already uses.

Everything this command creates is namespaced so a second run (or
`--reset`) can find and remove it without touching real `kairos_dev` data
a developer created by hand: every seeded `AppUser.email` ends in
`@perf.kairos.local`, and every seeded `Resource.name` starts with `PERF `.

Resource pools (Scope IN's own four measurement needs, each isolated so
one test's traffic can't corrupt another's):

- `PERF Dense N`  — a handful of resources booked near-solid across the
  next 92 days, for PERF-02(b)'s "near-fully-booked resource at the
  92-day query bound."
- `PERF Moderate N` — the bulk of the seeded resources (`--resources`),
  representative booking density (PRD A1) including held and cancelled
  rows, for PERF-02(a)'s ordinary-density read measurement.
- `PERF Write N` — a small pool kept entirely EMPTY, reserved for PERF-01
  (both the steady baseline and the 200-request spike need GUARANTEED
  free, non-conflicting slots — computing free slots against already-busy
  resources during a live latency measurement would add noise the
  measurement isn't supposed to include).
- `PERF CONC06` — exactly one resource, also kept entirely EMPTY, reserved
  for CONC-06's own escalating-writer-count exercise (Test Plan's own
  setup: "one resource... writers target distinct, non-overlapping slots
  on that resource").

Writes a JSON manifest (default: alongside this command's own working
directory unless `--manifest-path` overrides it) that `scripts/perf/`'s
load-testing scripts read — user/resource UUIDs only, no secrets, since
each script mints its own fresh session tokens at start via the real
`kairos.identity.oidc.issue_session_token` rather than persisting
long-lived tokens to disk.
"""

from __future__ import annotations

import json
import random
import uuid
from datetime import time, timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import IntegrityError, transaction
from django.utils import timezone as django_timezone

from kairos.bookings.models import Booking, BookingStatus
from kairos.identity.models import AppUser
from kairos.resources.models import Resource
from kairos.waitlist.models import WaitlistEntry, WaitlistOffer

PERF_EMAIL_DOMAIN = "@perf.kairos.local"
PERF_NAME_PREFIX = "PERF "

DENSE_RESOURCE_COUNT = 5
WRITE_POOL_RESOURCE_COUNT = 15
CONC06_RESOURCE_COUNT = 1


class Command(BaseCommand):
    help = (
        "Seed a PRD A1-scale dataset (hundreds of resources, thousands of users, "
        "representative booking density) for Phase 29 performance/load testing. "
        "Idempotent-ish via namespacing (--reset removes only PERF-prefixed rows)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--users", type=int, default=2000)
        parser.add_argument("--resources", type=int, default=280, help="'Moderate' pool size")
        parser.add_argument(
            "--reset", action="store_true", help="Delete all previously-seeded PERF rows first"
        )
        parser.add_argument(
            "--manifest-path",
            type=str,
            default="perf_manifest.json",
            help="Where to write the resource/user UUID manifest the load scripts read",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["reset"]:
            self._reset()

        n_users = options["users"]
        n_moderate = options["resources"]

        self.stdout.write(f"Seeding {n_users} users...")
        owner, users = self._seed_users(n_users)

        self.stdout.write(f"Seeding {DENSE_RESOURCE_COUNT} dense resources...")
        dense = self._seed_resources(owner, DENSE_RESOURCE_COUNT, "Dense")
        for r in dense:
            self._fill_dense(r, users)

        self.stdout.write(f"Seeding {n_moderate} moderate-density resources...")
        moderate = self._seed_resources(owner, n_moderate, "Moderate")
        for i, r in enumerate(moderate):
            self._fill_moderate(r, users)
            if (i + 1) % 50 == 0:
                self.stdout.write(f"  ...{i + 1}/{n_moderate}")

        self.stdout.write(f"Seeding {WRITE_POOL_RESOURCE_COUNT} write-pool resources (empty)...")
        write_pool = self._seed_resources(owner, WRITE_POOL_RESOURCE_COUNT, "Write")

        self.stdout.write("Seeding the CONC-06 resource (kept empty)...")
        conc06 = self._seed_resources(owner, CONC06_RESOURCE_COUNT, "CONC06")

        manifest = {
            "owner_user_id": str(owner.id),
            "users": [str(u.id) for u in users],
            "dense_resource_ids": [str(r.id) for r in dense],
            "moderate_resource_ids": [str(r.id) for r in moderate],
            "write_pool_resource_ids": [str(r.id) for r in write_pool],
            "conc06_resource_id": str(conc06[0].id),
        }
        with open(options["manifest_path"], "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {len(users)} users, "
                f"{len(dense) + len(moderate) + len(write_pool) + len(conc06)} resources. "
                f"Manifest: {options['manifest_path']}"
            )
        )

    # ------------------------------------------------------------------

    def _reset(self) -> None:
        self.stdout.write("Removing previously-seeded PERF rows...")
        # WaitlistOffer.hold_booking is RESTRICT-on-delete, so any offer a
        # PERF-resource cascade produced (e.g. a prior perf_03 run) must go
        # before the Booking rows it references — otherwise Django's own
        # collector raises RestrictedError rather than silently cascading.
        deleted_offers = WaitlistOffer.objects.filter(
            resource__name__startswith=PERF_NAME_PREFIX
        ).delete()
        deleted_bookings = Booking.objects.filter(
            resource__name__startswith=PERF_NAME_PREFIX
        ).delete()
        deleted_entries = WaitlistEntry.objects.filter(
            resource__name__startswith=PERF_NAME_PREFIX
        ).delete()
        Resource.objects.filter(name__startswith=PERF_NAME_PREFIX).delete()
        AppUser.objects.filter(email__endswith=PERF_EMAIL_DOMAIN).delete()
        self.stdout.write(
            f"  offers={deleted_offers[0]} bookings={deleted_bookings[0]} "
            f"waitlist_entries={deleted_entries[0]}"
        )

    def _seed_users(self, n: int) -> tuple[AppUser, list[AppUser]]:
        # A dedicated owner/created_by principal for every seeded resource —
        # Resource.created_by is RESTRICT-on-delete, so a single stable
        # owner (rather than a random seeded user) keeps --reset simple:
        # delete all PERF users only AFTER their resources are gone.
        owner, _ = AppUser.objects.get_or_create(
            email=f"perf-owner{PERF_EMAIL_DOMAIN}", defaults={"display_name": "Perf Owner"}
        )
        existing = list(
            AppUser.objects.filter(email__endswith=PERF_EMAIL_DOMAIN).exclude(id=owner.id)
        )
        if len(existing) >= n:
            return owner, existing[:n]

        to_create = [
            AppUser(
                id=uuid.uuid4(),
                email=f"perf-user-{i}{PERF_EMAIL_DOMAIN}",
                display_name=f"Perf User {i}",
            )
            for i in range(len(existing), n)
        ]
        AppUser.objects.bulk_create(to_create, batch_size=500)
        return owner, existing + to_create

    def _seed_resources(self, owner: AppUser, n: int, label: str) -> list[Resource]:
        return [
            Resource.objects.create(
                name=f"{PERF_NAME_PREFIX}{label} {i}",
                timezone="UTC",
                bookable_start_time=time(0, 0),
                bookable_end_time=time(23, 59, 59),
                created_by=owner,
            )
            for i in range(n)
        ]

    def _fill_dense(self, resource: Resource, users: list[AppUser]) -> None:
        """Near-fully-booked across the next 92 days (PERF-02(b)'s own
        bound) — back-to-back 1-hour confirmed bookings with an occasional
        deliberate gap (never literally 100% full, matching "near-fully-
        booked" rather than an unrealistic total lock-out).
        """
        tomorrow = django_timezone.now() + timedelta(days=1)
        start_day = tomorrow.replace(minute=0, second=0, microsecond=0)
        with transaction.atomic():
            cursor = start_day
            end_bound = start_day + timedelta(days=92)
            while cursor < end_bound:
                if random.random() < 0.08:  # ~8% of hours left free
                    cursor += timedelta(hours=1)
                    continue
                Booking.objects.create(
                    resource=resource,
                    user=random.choice(users),
                    time_range=(cursor, cursor + timedelta(hours=1)),
                    status=BookingStatus.CONFIRMED,
                )
                cursor += timedelta(hours=1)

    def _fill_moderate(self, resource: Resource, users: list[AppUser]) -> None:
        """Representative density (PRD A1): a modest number of confirmed
        bookings scattered across the next 60 days, plus a few cancelled
        and held rows, so the audit trail and holds/cancellations aren't
        empty either. Overlap collisions from random placement are
        expected occasionally at this density — caught and skipped rather
        than retried indefinitely, since a slightly-lower count on one
        resource doesn't matter for a representative-density dataset.
        """
        now = django_timezone.now()
        n_confirmed = random.randint(15, 40)
        n_cancelled = random.randint(0, 5)
        n_held = random.randint(0, 2)

        with transaction.atomic():
            for _ in range(n_confirmed):
                start = self._random_slot(now, 60)
                try:
                    with transaction.atomic():
                        Booking.objects.create(
                            resource=resource,
                            user=random.choice(users),
                            time_range=(start, start + timedelta(minutes=random.choice([30, 60]))),
                            status=BookingStatus.CONFIRMED,
                        )
                except IntegrityError:
                    continue  # exclusion-constraint collision — skip, not fatal

            for _ in range(n_cancelled):
                start = self._random_slot(now, 60)
                canceller = random.choice(users)
                try:
                    with transaction.atomic():
                        Booking.objects.create(
                            resource=resource,
                            user=random.choice(users),
                            time_range=(start, start + timedelta(minutes=30)),
                            status=BookingStatus.CANCELLED,
                            cancelled_at=now,
                            cancelled_by=canceller,
                            cancellation_reason="perf seed data",
                        )
                except IntegrityError:
                    continue

            for _ in range(n_held):
                start = self._random_slot(now, 60)
                try:
                    with transaction.atomic():
                        Booking.objects.create(
                            resource=resource,
                            user=random.choice(users),
                            time_range=(start, start + timedelta(minutes=30)),
                            status=BookingStatus.HELD,
                            expires_at=now + timedelta(minutes=random.randint(1, 15)),
                        )
                except IntegrityError:
                    continue

    @staticmethod
    def _random_slot(now: Any, horizon_days: int) -> Any:
        day_offset = random.randint(1, horizon_days)
        hour = random.randint(0, 22)
        minute = random.choice([0, 30])
        return (now + timedelta(days=day_offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
