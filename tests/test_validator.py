"""Tests for static validation.

These aren't here to raise a coverage number. Each case is a failure I either
hit or expect to hit: SQL that must be refused, SQL that must be allowed, and
the awkward middle where a conservative validator should stay quiet rather than
block something valid.
"""

from __future__ import annotations

import pytest

from queryguard.models import IssueCode
from queryguard.schema.catalog import Catalog, Column, ForeignKey, Table
from queryguard.validate.policy import enforce_limit
from queryguard.validate.static_checks import validate


@pytest.fixture
def catalog() -> Catalog:
    users = Table(
        name="users",
        columns=[
            Column("user_id", "INTEGER", primary_key=True),
            Column("account_id", "INTEGER"),
            Column("email", "TEXT"),
            Column("created_at", "TEXT"),
            Column("activated_at", "TEXT"),
            Column("deleted_at", "TEXT"),
        ],
        foreign_keys=[ForeignKey("users", "account_id", "accounts", "account_id")],
    )
    accounts = Table(
        name="accounts",
        columns=[
            Column("account_id", "INTEGER", primary_key=True),
            Column("company_name", "TEXT"),
            Column("region", "TEXT"),
            Column("churned_at", "TEXT"),
        ],
    )
    return Catalog(tables={"users": users, "accounts": accounts})


# --- things that must be refused -------------------------------------------

@pytest.mark.parametrize("sql,code", [
    ("DELETE FROM users", IssueCode.WRITE_STATEMENT),
    ("UPDATE users SET email = 'x'", IssueCode.WRITE_STATEMENT),
    ("INSERT INTO users (user_id) VALUES (1)", IssueCode.WRITE_STATEMENT),
    ("DROP TABLE users", IssueCode.WRITE_STATEMENT),
    ("CREATE TABLE evil (id INT)", IssueCode.WRITE_STATEMENT),
    ("ALTER TABLE users ADD COLUMN x TEXT", IssueCode.WRITE_STATEMENT),
    ("PRAGMA table_info(users)", IssueCode.FORBIDDEN_CONSTRUCT),
    ("ATTACH DATABASE '/tmp/x.db' AS x", IssueCode.FORBIDDEN_CONSTRUCT),
])
def test_non_select_statements_are_refused(sql, code, catalog):
    verdict = validate(sql, catalog)
    assert not verdict.ok
    assert any(i.code == code for i in verdict.issues), verdict.feedback


def test_write_hidden_after_a_select_is_refused(catalog):
    """The classic injection shape: a valid SELECT followed by something else."""
    verdict = validate("SELECT 1 FROM users; DROP TABLE users", catalog)
    assert not verdict.ok
    assert verdict.issues[0].code in (
        IssueCode.MULTIPLE_STATEMENTS, IssueCode.WRITE_STATEMENT)


def test_cte_wrapping_a_write_is_refused(catalog):
    verdict = validate(
        "WITH x AS (DELETE FROM users RETURNING user_id) SELECT * FROM x", catalog)
    assert not verdict.ok


def test_select_into_is_refused(catalog):
    verdict = validate("SELECT * INTO backup FROM users", catalog)
    assert not verdict.ok


# --- hallucinated identifiers ----------------------------------------------

def test_unknown_table(catalog):
    verdict = validate("SELECT * FROM subscriptions", catalog)
    assert not verdict.ok
    assert verdict.issues[0].code == IssueCode.UNKNOWN_TABLE


def test_unknown_qualified_column_names_the_table(catalog):
    verdict = validate("SELECT u.is_active FROM users u", catalog)
    assert not verdict.ok
    issue = verdict.issues[0]
    assert issue.code == IssueCode.UNKNOWN_COLUMN
    assert issue.identifier == "users.is_active"


def test_unknown_unqualified_column(catalog):
    verdict = validate("SELECT is_active FROM users", catalog)
    assert not verdict.ok
    assert verdict.issues[0].code == IssueCode.UNKNOWN_COLUMN


def test_typo_gets_a_suggestion(catalog):
    """The suggestion is the point -- it goes straight into the repair prompt."""
    verdict = validate("SELECT u.activated_ad FROM users u", catalog)
    assert not verdict.ok
    assert "did you mean activated_at?" in verdict.feedback


def test_unknown_alias_is_reported(catalog):
    verdict = validate("SELECT x.email FROM users u", catalog)
    assert not verdict.ok
    assert verdict.issues[0].code == IssueCode.UNKNOWN_TABLE


def test_duplicate_bad_identifier_reported_once(catalog):
    verdict = validate(
        "SELECT u.nope FROM users u WHERE u.nope > 1 ORDER BY u.nope", catalog)
    assert len([i for i in verdict.issues if i.code == IssueCode.UNKNOWN_COLUMN]) == 1


# --- valid SQL must survive -------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM users",
    "SELECT * FROM users WHERE deleted_at IS NULL",
    "SELECT a.region, COUNT(*) FROM accounts a JOIN users u ON a.account_id = u.account_id GROUP BY a.region",
    "SELECT region, COUNT(*) AS total FROM accounts GROUP BY region ORDER BY total DESC",
    "SELECT COUNT(*) FROM users WHERE account_id IN (SELECT account_id FROM accounts WHERE region = 'NA')",
    "WITH active AS (SELECT * FROM users WHERE deleted_at IS NULL) SELECT COUNT(*) FROM active",
    "SELECT u.email FROM users u UNION SELECT a.company_name FROM accounts a",
])
def test_valid_queries_pass(sql, catalog):
    verdict = validate(sql, catalog)
    assert verdict.ok, verdict.feedback


def test_output_alias_is_referenceable(catalog):
    """`total` isn't a column on any table; it's defined in the SELECT list."""
    verdict = validate(
        "SELECT COUNT(*) AS total FROM users GROUP BY account_id HAVING total > 2", catalog)
    assert verdict.ok, verdict.feedback


def test_derived_table_columns_are_not_flagged(catalog):
    """Conservative by design: a column from a derived table is unresolvable
    here, so the validator stays quiet rather than blocking valid SQL."""
    verdict = validate(
        "SELECT t.n FROM (SELECT COUNT(*) AS n FROM users) t", catalog)
    assert verdict.ok, verdict.feedback


# --- policy -----------------------------------------------------------------

def test_limit_is_added_when_missing():
    sql, added = enforce_limit("SELECT * FROM users", max_rows=200)
    assert added and "LIMIT 200" in sql.upper()


def test_existing_limit_is_left_alone():
    """Rewriting the model's own LIMIT would change the answer to 'top 500'."""
    sql, added = enforce_limit("SELECT * FROM users LIMIT 500", max_rows=200)
    assert not added and "500" in sql
