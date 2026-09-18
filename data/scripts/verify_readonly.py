#!/usr/bin/env python3
"""Prove the Postgres read-only boundary actually holds.

The static validator in src/queryguard/validate/ is a usability feature: it
catches mistakes early and gives the repair loop something to work with. It is
not the security boundary, because it is code I wrote and code I wrote has bugs.

The security boundary is the database role. This script tries to break it.
Every statement here must fail. If any of them succeeds, the claim in the
README is false and I want to know before an interviewer does.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

ATTACKS = [
    ("INSERT", (
        "INSERT INTO accounts (account_id, company_name, region, signed_up_at, "
        "current_plan_id, seats_purchased) VALUES (999999, 'x', 'NA', now(), 1, 1)"
    )),
    ("UPDATE", "UPDATE accounts SET company_name = 'owned' WHERE account_id = 1"),
    ("DELETE", "DELETE FROM accounts WHERE account_id = 1"),
    ("TRUNCATE", "TRUNCATE accounts"),
    ("DROP", "DROP TABLE events"),
    ("CREATE TABLE", "CREATE TABLE evil (id INTEGER)"),
    ("CREATE TEMP TABLE", "CREATE TEMP TABLE scratch (id INTEGER)"),
    ("ALTER", "ALTER TABLE accounts ADD COLUMN pwned TEXT"),
    ("GRANT", "GRANT ALL ON accounts TO queryguard_ro"),
    ("COPY TO FILE", "COPY accounts TO '/tmp/leak.csv'"),
]


def main() -> int:
    import psycopg

    from queryguard.config import get_settings

    dsn = get_settings().pg_dsn
    if not dsn:
        print("QG_PG_DSN not set", file=sys.stderr)
        return 2
    if "queryguard_ro" not in dsn:
        print("refusing to run: QG_PG_DSN is not the read-only role", file=sys.stderr)
        return 2

    failures = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM accounts")
            print(f"SELECT works: {cur.fetchone()[0]} accounts readable\n")
            cur.execute("SHOW statement_timeout")
            print(f"statement_timeout = {cur.fetchone()[0]}\n")

        for name, sql in ATTACKS:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                conn.commit()
                failures.append(name)
                print(f"  {name:<20} SUCCEEDED  <-- BOUNDARY BROKEN")
            except psycopg.Error as exc:
                conn.rollback()
                reason = str(exc).splitlines()[0]
                print(f"  {name:<20} blocked    ({reason[:60]})")

    print()
    if failures:
        print(f"FAIL: {len(failures)} statement(s) got through: {', '.join(failures)}")
        return 1
    print(f"PASS: all {len(ATTACKS)} write attempts blocked at the database level")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
