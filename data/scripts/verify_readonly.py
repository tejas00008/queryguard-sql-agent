#!/usr/bin/env python3
"""Prove the Postgres read-only boundary actually holds.

The static validator in src/queryguard/validate/ is a usability feature: it
catches mistakes early and gives the repair loop something to work with. It is
not the security boundary, because it is code I wrote and code I wrote has bugs.

The security boundary is the database role. This script tries to break it, in
two phases:

  Phase 1 -- a normal session. Everything should be refused by
             default_transaction_read_only.

  Phase 2 -- the same attacks after `SET default_transaction_read_only = off`.
             A role may override its own default, so phase 1 alone proves very
             little. Phase 2 is what actually tests the grants.

Two things I got wrong writing this, both worth keeping in mind:

1. Phase 2 must run on an autocommit connection. `SET default_transaction_
   read_only` only affects transactions started after it, so issuing it inside
   an open transaction does nothing. My first version did exactly that and then
   reported PASS, because everything was still blocked by the default it
   believed it had disabled. A green test that exercises nothing is worse than
   no test.

2. "Did the statement raise?" is the wrong pass criterion. A non-owner GRANT
   returns successfully and emits `WARNING: no privileges were granted` -- it
   changes nothing. So the check here is on effects: row counts, actual
   privileges, and whether any new table appeared.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

WATCHED_TABLES = ("accounts", "events", "users")
WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")

ATTACKS: list[tuple[str, str]] = [
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
    ("SELECT INTO TEMP", "CREATE TEMP TABLE stolen AS SELECT * FROM users"),
    ("ALTER", "ALTER TABLE accounts ADD COLUMN pwned TEXT"),
    ("GRANT", "GRANT ALL ON accounts TO queryguard_ro"),
    ("COPY TO FILE", "COPY accounts TO '/tmp/leak.csv'"),
]


def snapshot(conn) -> dict[str, object]:
    """Everything an attack could plausibly change."""
    state: dict[str, object] = {}
    with conn.cursor() as cur:
        for table in WATCHED_TABLES:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            state[f"rows:{table}"] = cur.fetchone()[0]
            for priv in WRITE_PRIVILEGES:
                cur.execute(
                    "SELECT has_table_privilege('queryguard_ro', %s, %s)", (table, priv)
                )
                state[f"priv:{table}:{priv}"] = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'"
        )
        state["table_count"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_name = 'accounts'")
        state["accounts_columns"] = cur.fetchone()[0]
    if not conn.autocommit:
        conn.rollback()
    return state


def run_phase(conn, label: str) -> list[str]:
    """Run every attack. Returns the names of any that actually changed state."""
    import psycopg

    print(f"\n--- {label} ---")
    before = snapshot(conn)
    silent: list[str] = []

    for name, sql in ATTACKS:
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            if not conn.autocommit:
                conn.commit()
            silent.append(name)
            print(f"  {name:<20} no error raised (checking for effect below)")
        except psycopg.Error as exc:
            if not conn.autocommit:
                conn.rollback()
            print(f"  {name:<20} blocked    ({str(exc).splitlines()[0][:56]})")

    after = snapshot(conn)
    changed = [k for k in before if before[k] != after[k]]
    if changed:
        print(f"  !! STATE CHANGED: {changed}")
        return silent
    if silent:
        print(f"  no-ops (returned successfully, changed nothing): {', '.join(silent)}")
    return []


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

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM accounts")
            print(f"SELECT works: {cur.fetchone()[0]} accounts readable")
            cur.execute("SHOW statement_timeout")
            print(f"statement_timeout = {cur.fetchone()[0]}")
        broken = run_phase(conn, "phase 1: normal session")

    # Autocommit is required here -- see reason 1 in the module docstring.
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = off")
            cur.execute("SHOW default_transaction_read_only")
            state = cur.fetchone()[0]
        if state != "off":
            print(f"\nABORT: could not disable the read-only default (still {state!r}); "
                  "phase 2 would be vacuous", file=sys.stderr)
            return 2
        broken += run_phase(
            conn, "phase 2: read-only default disabled, grants are all that remain"
        )

    print()
    if broken:
        print(f"FAIL: {', '.join(sorted(set(broken)))} changed database state")
        return 1
    print(f"PASS: {2 * len(ATTACKS)} write attempts, none changed any state")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
