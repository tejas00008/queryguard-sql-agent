"""Tests for the catalog and the sandboxed executor.

These run against the generated SaaS database and skip if it isn't built, so a
fresh clone doesn't fail before `make db-sqlite`.
"""

from __future__ import annotations

import pytest

from queryguard.execution.sandbox import SqliteExecutor
from queryguard.schema.catalog import load_sqlite_catalog

DB = "data/db/saas.sqlite"
pytestmark = pytest.mark.skipif(
    not __import__("pathlib").Path(DB).exists(),
    reason="run `make db-sqlite` first",
)


@pytest.fixture(scope="module")
def executor() -> SqliteExecutor:
    return SqliteExecutor(DB, max_rows=5, timeout_s=3)


def test_catalog_finds_every_table():
    catalog = load_sqlite_catalog(DB)
    assert len(catalog) == 9
    assert catalog.has_table("users")


def test_lookups_ignore_case():
    catalog = load_sqlite_catalog(DB)
    assert catalog.has_column("USERS", "Deleted_At")


def test_foreign_key_expansion_reaches_join_partners():
    """Retrieving `invoices` without `accounts` yields SQL that can't join."""
    catalog = load_sqlite_catalog(DB)
    assert "accounts" in catalog.neighbours({"invoices"})
    assert "subscriptions" in catalog.neighbours({"invoices"})


def test_row_cap_truncates_and_says_so(executor):
    result = executor.execute("SELECT * FROM events")
    assert result.ok
    assert result.row_count == 5
    assert result.truncated


def test_writes_are_refused_by_sqlite_itself(executor):
    """Not by the validator -- this is the connection being read-only."""
    result = executor.execute("DELETE FROM accounts")
    assert not result.ok
    assert "readonly" in result.error.lower()


def test_unknown_column_surfaces_the_database_error(executor):
    result = executor.execute("SELECT nope FROM accounts")
    assert not result.ok
    assert "nope" in result.error


def test_runaway_query_is_interrupted(executor):
    """Three-way cross join on 106k rows; must stop at the deadline."""
    result = executor.execute("SELECT COUNT(*) FROM events a, events b, events c")
    assert not result.ok
    assert "time limit" in result.error
    assert result.duration_ms < 6000


def test_successful_query_reports_columns(executor):
    result = executor.execute("SELECT region, COUNT(*) AS n FROM accounts GROUP BY region")
    assert result.ok
    assert result.columns == ["region", "n"]
    assert result.row_count == 4
