"""
Shared helpers for the S1 spike scripts (RFC v1.0 §16).

THROWAWAY CODE. This module and everything alongside it in scripts/spike/
exists only to produce docs/spikes/S1-postgres-verification.md. None of it
becomes the application. Real models/migrations start in Phase 2.
"""

from __future__ import annotations

import psycopg

DSN = "postgresql://kairos:kairos@localhost:5432/kairos_dev"

# Production write-path session settings, RFC v1.0 §7.1 / .env.example.
# The spike must observe behavior under the SAME settings production will run
# with, or the timings and blocking behavior it records are meaningless.
SESSION_SETTINGS_SQL = """
SET lock_timeout = '3s';
SET statement_timeout = '10s';
SET idle_in_transaction_session_timeout = '30s';
"""


def connect() -> psycopg.Connection:
    conn = psycopg.connect(DSN, autocommit=False)
    with conn.cursor() as cur:
        cur.execute(SESSION_SETTINGS_SQL)
    conn.commit()
    return conn


def reset_schema() -> None:
    """Drop and recreate a minimal booking table carrying the exact
    exclusion constraint from Spec v1.0 §3, so the spike tests the real
    mechanism rather than an approximation of it."""
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
        cur.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")
        cur.execute("DROP TABLE IF EXISTS spike_booking;")
        cur.execute(
            """
            CREATE TABLE spike_booking (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                resource_id UUID NOT NULL,
                time_range TSTZRANGE NOT NULL,
                status TEXT NOT NULL DEFAULT 'confirmed'
                    CHECK (status IN ('confirmed', 'held', 'cancelled')),
                expires_at TIMESTAMPTZ,
                CONSTRAINT spike_no_overlap
                    EXCLUDE USING gist (
                        resource_id WITH =,
                        time_range WITH &&
                    )
                    WHERE (status IN ('confirmed', 'held'))
            );
            """
        )
    conn.close()
