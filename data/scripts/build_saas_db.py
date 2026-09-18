#!/usr/bin/env python3
"""Generate the synthetic SaaS analytics database.

Why synthetic: I need a schema whose ambiguities I control. BIRD gives me
externally-verifiable accuracy numbers, but its schemas are whatever they are.
Here I can plant specific traps -- soft deletes, money in cents, a legacy event
name that was renamed mid-history, two defensible definitions of "churn" -- and
then write questions that step on them. That is how I test whether the agent
states its assumptions instead of silently picking one.

The row data is generated once, engine-independently, then loaded into SQLite
and/or Postgres so the same questions can be run against both.

Deterministic: same --seed gives byte-identical SQLite output.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

# Naive on purpose: SQLite has no tz-aware type, and this models a
# single-timezone warehouse. Keeping both engines naive means result sets
# compare cleanly across them.
ANCHOR = datetime(2026, 9, 18)  # noqa: DTZ001  -- "today" for the dataset
HISTORY_DAYS = 730
EVENT_RENAME_CUTOVER = ANCHOR - timedelta(days=150)

REGIONS = ["NA", "EMEA", "APAC", "LATAM"]
TICKET_SEVERITY = ["low", "medium", "high", "urgent"]
CHANNELS = ["paid_search", "paid_social", "content", "webinar", "partner"]

# Trap: 'login'/'project_create' were renamed to 'user_login'/'project_created'
# at the cutover. Any question about total logins has to union both names
# or it silently undercounts. Measured undercount is reported in
# data/README.md rather than guessed at here.
LEGACY_NAMES = {"login": "user_login", "project_create": "project_created"}
STABLE_EVENTS = ["dashboard_view", "report_export", "invite_sent", "api_call"]

COMPANY_A = ["North", "Blue", "Iron", "Swift", "Quiet", "Bright", "Nova", "Cedar",
             "Harbor", "Summit", "Vector", "Atlas", "Ember", "Rapid", "Clear"]
COMPANY_B = ["Labs", "Works", "Systems", "Digital", "Group", "Analytics", "Cloud",
             "Robotics", "Media", "Health", "Logistics", "Finance"]
FIRST = ["ana", "raj", "mei", "tom", "priya", "luis", "sara", "ken", "amara", "nils",
         "zoe", "omar", "hana", "ivan", "lena", "kofi", "ravi", "elsa", "dan", "yuki"]
LAST = ["patel", "silva", "chen", "novak", "okafor", "muller", "rossi", "kim",
        "haddad", "olsen", "dubois", "santos", "ahmed", "wang", "berg"]

PLANS = [
    (1, "free",       "Free",       0,      3),
    (2, "starter",    "Starter",    2900,   10),
    (3, "growth",     "Growth",     9900,   50),
    (4, "enterprise", "Enterprise", 49900, 500),
]

TABLES = ["plans", "campaigns", "accounts", "account_sources", "users",
          "subscriptions", "invoices", "support_tickets", "events"]

DDL = {
    "plans": """
        plan_id {int} PRIMARY KEY,
        code {txt} NOT NULL,
        name {txt} NOT NULL,
        monthly_price_cents {int} NOT NULL,
        seat_limit {int} NOT NULL""",
    "campaigns": """
        campaign_id {int} PRIMARY KEY,
        name {txt} NOT NULL,
        channel {txt} NOT NULL,
        started_at {ts} NOT NULL,
        ended_at {ts},
        budget_cents {int} NOT NULL""",
    "accounts": """
        account_id {int} PRIMARY KEY,
        company_name {txt} NOT NULL,
        region {txt} NOT NULL,
        signed_up_at {ts} NOT NULL,
        trial_ends_at {ts},
        churned_at {ts},
        current_plan_id {int} REFERENCES plans(plan_id),
        seats_purchased {int} NOT NULL""",
    "account_sources": """
        account_id {int} PRIMARY KEY REFERENCES accounts(account_id),
        campaign_id {int} REFERENCES campaigns(campaign_id),
        attributed_at {ts} NOT NULL,
        touch_type {txt} NOT NULL""",
    "users": """
        user_id {int} PRIMARY KEY,
        account_id {int} NOT NULL REFERENCES accounts(account_id),
        email {txt} NOT NULL,
        full_name {txt} NOT NULL,
        role {txt} NOT NULL,
        created_at {ts} NOT NULL,
        activated_at {ts},
        last_seen_at {ts},
        deleted_at {ts}""",
    "subscriptions": """
        subscription_id {int} PRIMARY KEY,
        account_id {int} NOT NULL REFERENCES accounts(account_id),
        plan_id {int} NOT NULL REFERENCES plans(plan_id),
        started_at {ts} NOT NULL,
        ended_at {ts},
        status {txt} NOT NULL,
        mrr_cents {int} NOT NULL""",
    "invoices": """
        invoice_id {int} PRIMARY KEY,
        account_id {int} NOT NULL REFERENCES accounts(account_id),
        subscription_id {int} NOT NULL REFERENCES subscriptions(subscription_id),
        issued_at {ts} NOT NULL,
        paid_at {ts},
        amount_cents {int} NOT NULL,
        status {txt} NOT NULL""",
    "support_tickets": """
        ticket_id {int} PRIMARY KEY,
        account_id {int} NOT NULL REFERENCES accounts(account_id),
        opened_by_user_id {int} NOT NULL REFERENCES users(user_id),
        opened_at {ts} NOT NULL,
        resolved_at {ts},
        severity {txt} NOT NULL,
        csat_score {int}""",
    "events": """
        event_id {int} PRIMARY KEY,
        user_id {int} NOT NULL REFERENCES users(user_id),
        account_id {int} NOT NULL REFERENCES accounts(account_id),
        event_name {txt} NOT NULL,
        occurred_at {ts} NOT NULL,
        properties {txt}""",
}

INDEXES = [
    ("events", "user_id"), ("events", "account_id"), ("events", "occurred_at"),
    ("users", "account_id"), ("invoices", "account_id"),
    ("subscriptions", "account_id"), ("support_tickets", "account_id"),
]


def ddl_for(engine: str, table: str) -> str:
    types = (
        {"int": "INTEGER", "txt": "TEXT", "ts": "TEXT"}
        if engine == "sqlite"
        else {"int": "INTEGER", "txt": "TEXT", "ts": "TIMESTAMP"}
    )
    body = DDL[table].format(**types)
    return f"CREATE TABLE {table} ({body}\n)"


def rand_dt(rng: random.Random, lo: datetime, hi: datetime) -> datetime:
    if hi <= lo:
        return lo
    secs = int((hi - lo).total_seconds())
    return lo + timedelta(seconds=rng.randrange(secs))


def build(seed: int) -> dict[str, list[tuple]]:
    rng = random.Random(seed)
    history_start = ANCHOR - timedelta(days=HISTORY_DAYS)
    data: dict[str, list[tuple]] = {t: [] for t in TABLES}

    data["plans"] = list(PLANS)
    plan_by_id = {p[0]: p for p in PLANS}

    for cid in range(1, 13):
        started = rand_dt(rng, history_start, ANCHOR - timedelta(days=30))
        ended = started + timedelta(days=rng.randrange(30, 180))
        data["campaigns"].append((
            cid,
            f"{rng.choice(['Q1', 'Q2', 'Q3', 'Q4'])} {rng.choice(CHANNELS).replace('_', ' ').title()} {started.year}",
            rng.choice(CHANNELS), started,
            ended if ended < ANCHOR else None,
            rng.randrange(50, 800) * 10000,
        ))

    user_id = 0
    sub_id = 0
    invoice_id = 0
    event_id = 0
    ticket_id = 0

    for account_id in range(1, 401):
        signed_up = rand_dt(rng, history_start, ANCHOR - timedelta(days=20))
        plan_id = rng.choices([1, 2, 3, 4], weights=[25, 40, 25, 10])[0]
        plan = plan_by_id[plan_id]
        seats = max(1, min(plan[4], rng.randrange(1, 14)))

        # Trap: churned_at on the account and the subscription's ended_at are
        # set from the same event, but ~6% of churned accounts never got the
        # account flag backfilled. "How many customers churned?" has two
        # defensible answers and they differ.
        churned_at = None
        if rng.random() < 0.22:
            c = signed_up + timedelta(days=rng.randrange(60, 600))
            if c < ANCHOR:
                churned_at = c

        data["accounts"].append((
            account_id,
            f"{rng.choice(COMPANY_A)}{rng.choice(COMPANY_B)}",
            rng.choice(REGIONS), signed_up,
            signed_up + timedelta(days=14),
            None if (churned_at and rng.random() < 0.06) else churned_at,
            plan_id, seats,
        ))

        # Trap: ~25% of accounts have no attribution row at all (organic).
        # Any INNER JOIN to campaigns silently drops a quarter of the base.
        if rng.random() < 0.75:
            data["account_sources"].append((
                account_id, rng.randrange(1, 13),
                signed_up - timedelta(days=rng.randrange(0, 30)),
                rng.choice(["first_touch", "last_touch"]),
            ))

        account_end = churned_at or ANCHOR
        account_users = []
        for n in range(max(1, min(seats + rng.randrange(-1, 4), 14))):
            user_id += 1
            created = rand_dt(rng, signed_up, account_end)
            # Trap: created_at is when they were invited, activated_at is when
            # they first logged in. ~18% never activate. "New users this month"
            # means different things depending on which you pick.
            # A user invited right before the account's end boundary simply
            # hasn't activated yet. Clamping to the boundary instead would pin
            # a pile of users and their events to one identical timestamp.
            latest_activation = account_end - timedelta(days=1)
            activated = None
            if rng.random() < 0.82 and created < latest_activation:
                activated = min(
                    created + timedelta(days=rng.randrange(0, 8)), latest_activation
                )
            last_seen = rand_dt(rng, activated, account_end) if activated else None
            # Trap: soft delete. Every "active users" query must exclude these.
            deleted = rand_dt(rng, created, account_end) if rng.random() < 0.08 else None
            fn, ln = rng.choice(FIRST), rng.choice(LAST)
            data["users"].append((
                user_id, account_id,
                f"{fn}.{ln}{user_id}@{data['accounts'][-1][1].lower()}.com",
                f"{fn.title()} {ln.title()}",
                "owner" if n == 0 else rng.choices(["admin", "member"], weights=[1, 4])[0],
                created, activated, last_seen, deleted,
            ))
            account_users.append((user_id, activated, last_seen, deleted))

        # Subscriptions: one, plus an upgrade for some accounts.
        segments = [(plan_id, signed_up)]
        if rng.random() < 0.3 and plan_id < 4:
            upgrade_at = signed_up + timedelta(days=rng.randrange(45, 400))
            if upgrade_at < account_end:
                segments.append((plan_id + 1, upgrade_at))

        for idx, (seg_plan, seg_start) in enumerate(segments):
            sub_id += 1
            is_last = idx == len(segments) - 1
            seg_end = None if is_last else segments[idx + 1][1]
            if is_last and churned_at:
                seg_end = churned_at
            if is_last:
                status = "canceled" if churned_at else rng.choices(
                    ["active", "past_due"], weights=[9, 1])[0]
            else:
                status = "upgraded"
            mrr = plan_by_id[seg_plan][3] * seats
            data["subscriptions"].append(
                (sub_id, account_id, seg_plan, seg_start, seg_end, status, mrr))

            # Monthly invoices while the segment is live.
            cursor = seg_start
            stop = seg_end or ANCHOR
            while cursor < stop and mrr > 0:
                invoice_id += 1
                paid = None
                st = rng.choices(["paid", "open", "void"], weights=[88, 9, 3])[0]
                if st == "paid":
                    paid = cursor + timedelta(days=rng.randrange(0, 21))
                    if paid > ANCHOR:
                        paid, st = None, "open"
                data["invoices"].append(
                    (invoice_id, account_id, sub_id, cursor, paid, mrr, st))
                cursor += timedelta(days=30)

        # Events, only for activated users.
        for uid, activated, last_seen, _deleted in account_users:
            if not activated:
                continue
            upper = last_seen or account_end
            for _ in range(rng.randrange(8, 90)):
                event_id += 1
                occurred = rand_dt(rng, activated, upper)
                if rng.random() < 0.45:
                    base = rng.choice(list(LEGACY_NAMES))
                    name = base if occurred < EVENT_RENAME_CUTOVER else LEGACY_NAMES[base]
                else:
                    name = rng.choice(STABLE_EVENTS)
                data["events"].append((
                    event_id, uid, account_id, name, occurred,
                    json.dumps({"source": rng.choice(["web", "api", "mobile"])}),
                ))

        for _ in range(rng.randrange(0, 5)):
            ticket_id += 1
            opened = rand_dt(rng, signed_up, account_end)
            resolved = opened + timedelta(hours=rng.randrange(1, 400)) if rng.random() < 0.85 else None
            if resolved and resolved > ANCHOR:
                resolved = None
            data["support_tickets"].append((
                ticket_id, account_id, rng.choice(account_users)[0], opened, resolved,
                rng.choices(TICKET_SEVERITY, weights=[4, 5, 3, 1])[0],
                rng.randrange(1, 6) if (resolved and rng.random() < 0.6) else None,
            ))

    return data


def to_sqlite(data: dict[str, list[tuple]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for table in TABLES:
            conn.execute(ddl_for("sqlite", table))
            rows = [
                tuple(v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v
                      for v in row)
                for row in data[table]
            ]
            if rows:
                ph = ",".join("?" * len(rows[0]))
                conn.executemany(f"INSERT INTO {table} VALUES ({ph})", rows)
        for table, col in INDEXES:
            conn.execute(f"CREATE INDEX idx_{table}_{col} ON {table}({col})")
        conn.commit()
    finally:
        conn.close()


def to_postgres(data: dict[str, list[tuple]], dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for table in reversed(TABLES):
            cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        for table in TABLES:
            cur.execute(ddl_for("postgres", table))
            rows = data[table]
            if rows:
                cols = ",".join("%s" for _ in rows[0])
                with cur.copy(f"COPY {table} FROM STDIN") as copy:
                    for row in rows:
                        copy.write_row(row)
                del cols
        for table, col in INDEXES:
            cur.execute(f"CREATE INDEX idx_{table}_{col} ON {table}({col})")
        conn.commit()


def main() -> int:
    from queryguard.config import get_settings

    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", choices=["sqlite", "postgres", "both"], default="sqlite")
    ap.add_argument("--seed", type=int, default=settings.seed)
    ap.add_argument("--db-id", default="saas")
    args = ap.parse_args()

    data = build(args.seed)
    print(f"generated with seed={args.seed}")
    for table in TABLES:
        print(f"  {table:<18} {len(data[table]):>7,}")

    if args.target in ("sqlite", "both"):
        path = settings.sqlite_path(args.db_id)
        to_sqlite(data, path)
        print(f"sqlite   -> {path}  ({path.stat().st_size / 1e6:.1f} MB)")

    if args.target in ("postgres", "both"):
        dsn = settings.pg_admin_dsn
        if not dsn:
            print("QG_PG_ADMIN_DSN not set; skipping postgres", file=sys.stderr)
            return 1
        to_postgres(data, dsn)
        print(f"postgres -> {dsn.rsplit('@', 1)[-1]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
