"""
S1.1 — btree_gist availability. PROJECT-BLOCKING (RFC v1.0 §16, PRD C3).

If this fails against the actual deployment target, Candidate A (the
exclusion constraint) is void and the RFC must be reopened for Candidate D
(SERIALIZABLE/SSI). This script checks the local Docker target; the
production target must be checked separately before Phase 30.
"""

import psycopg

from common import DSN


def main() -> None:
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")
        cur.execute(
            "SELECT extname, extversion FROM pg_extension WHERE extname = 'btree_gist';"
        )
        row = cur.fetchone()
        cur.execute("SHOW server_version;")
        pg_version = cur.fetchone()[0]

    print(f"Postgres server_version: {pg_version}")
    if row:
        print(f"btree_gist: AVAILABLE (extname={row[0]}, extversion={row[1]})")
    else:
        print("btree_gist: NOT AVAILABLE")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
