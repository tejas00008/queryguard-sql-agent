"""Lock the documented traps in data/README.md.

The README presents these magnitudes as measured rather than asserted. That is
only honest if something re-measures them, so this file does. If the generator
changes and a trap quietly stops biting, these fail instead of the README
slowly becoming fiction.
"""

from __future__ import annotations

import sqlite3

import pytest

DB = "data/db/saas.sqlite"
TODAY = "2026-09-18"
# The README quotes 797 vs 725 active users. That pair is only reproducible at
# a 31-day window, which the README did not state; without it the numbers look
# wrong. Recorded here so the claim stays checkable.
ACTIVE_WINDOW_DAYS = 31


@pytest.fixture(scope="module")
def db():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    yield conn
    conn.close()


def scalar(db, sql: str) -> int:
    return db.execute(sql).fetchone()[0]


def test_row_counts(db):
    expected = {
        "plans": 4, "campaigns": 12, "accounts": 400, "account_sources": 297,
        "users": 2660, "subscriptions": 475, "invoices": 3523,
        "support_tickets": 788, "events": 106464,
    }
    actual = {t: scalar(db, f"SELECT COUNT(*) FROM {t}") for t in expected}
    assert actual == expected


def test_event_rename_trap(db):
    """`login` was renamed `user_login`; both names live in history."""
    naive = scalar(db, "SELECT COUNT(*) FROM events WHERE event_name='user_login'")
    correct = scalar(db, "SELECT COUNT(*) FROM events "
                         "WHERE event_name IN ('login','user_login')")
    assert (naive, correct) == (15440, 24270)
    assert round(100 * (1 - naive / correct)) == 36, "documented as a 36% undercount"


def test_soft_delete_trap(db):
    window = f"date('{TODAY}','-{ACTIVE_WINDOW_DAYS} day')"
    naive = scalar(db, f"SELECT COUNT(*) FROM users WHERE last_seen_at >= {window}")
    correct = scalar(db, "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL "
                         f"AND last_seen_at >= {window}")
    assert (naive, correct) == (797, 725)
    assert round(100 * (naive / correct - 1)) == 10, "documented as a 10% overcount"


def test_two_churn_definitions_disagree(db):
    by_account = scalar(db, "SELECT COUNT(*) FROM accounts WHERE churned_at IS NOT NULL")
    by_subscription = scalar(db, "SELECT COUNT(DISTINCT account_id) FROM subscriptions "
                                 "WHERE status='canceled'")
    assert (by_account, by_subscription) == (52, 55)


def test_missing_attribution_trap(db):
    total = scalar(db, "SELECT COUNT(*) FROM accounts")
    joined = scalar(db, "SELECT COUNT(*) FROM accounts a "
                        "JOIN account_sources s ON s.account_id = a.account_id")
    assert (total, joined) == (400, 297)
    assert total - joined == 103, "an INNER JOIN drops 103 organic accounts"


def test_invited_is_not_activated(db):
    total = scalar(db, "SELECT COUNT(*) FROM users")
    never = scalar(db, "SELECT COUNT(*) FROM users WHERE activated_at IS NULL")
    assert (total, never) == (2660, 486)


def test_money_is_stored_in_cents(db):
    """Reporting these raw is the 100x error the README describes."""
    cols = {
        row[1]
        for table in ("invoices", "subscriptions", "plans")
        for row in db.execute(f"PRAGMA table_info({table})")
    }
    assert {"amount_cents", "mrr_cents", "monthly_price_cents"} <= cols


def test_nothing_occurs_after_the_fixed_today(db):
    latest = db.execute("SELECT MAX(occurred_at) FROM events").fetchone()[0]
    assert latest[:10] <= TODAY
