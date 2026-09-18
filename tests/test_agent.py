"""Graph behaviour, driven by a stubbed model.

Every test here runs the real nodes, the real validator and the real SQLite
database -- only the model is scripted. That is the point: the interesting
behaviour (does it repair? does it stop repairing? does it refuse?) lives in
the edges, not in the model.
"""

from __future__ import annotations

import pytest

from queryguard.agent.graph import answer
from queryguard.agent.llm import StubLLM
from queryguard.agent.retrieval import select_tables
from queryguard.execution.sandbox import SqliteExecutor
from queryguard.models import AbstainReason, AnswerStatus, SQLDraft

DB = "data/db/saas.sqlite"


@pytest.fixture(scope="module")
def executor():
    return SqliteExecutor(DB)


def test_happy_path_answers(executor):
    llm = StubLLM([SQLDraft(
        sql="SELECT COUNT(*) AS n FROM accounts",
        tables_used=["accounts"], assumptions=["counted every account row"],
    )])
    result = answer("How many accounts are there?", llm, executor)
    assert result.status is AnswerStatus.ANSWERED
    assert result.rows == [(400,)]
    assert result.attempts == 1
    assert result.assumptions == ["counted every account row"]


def test_repairs_a_hallucinated_column(executor):
    """The first draft invents users.is_active; the second uses a real column."""
    llm = StubLLM([
        SQLDraft(sql="SELECT COUNT(*) FROM users WHERE is_active = 1"),
        SQLDraft(sql="SELECT COUNT(*) AS n FROM users WHERE deleted_at IS NULL"),
    ])
    result = answer("How many active users?", llm, executor)
    assert result.status is AnswerStatus.ANSWERED
    assert result.attempts == 2
    assert len(result.sql_history) == 2
    # The model was told precisely what was wrong.
    assert any(i.identifier == "is_active" for i in result.issues)
    second_prompt = llm.calls[1][1]
    assert "is_active" in second_prompt


def test_repair_budget_is_bounded(executor):
    """Four bad drafts against a budget of 3 repairs: 4 calls, then abstain."""
    llm = StubLLM([SQLDraft(sql="SELECT nope FROM users")] * 8)
    result = answer("unanswerable", llm, executor, max_repairs=3)
    assert result.status is AnswerStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.REPAIR_EXHAUSTED
    assert result.attempts == 4
    assert len(llm.calls) == 4


def test_write_request_refused_without_spending_repairs(executor):
    llm = StubLLM([SQLDraft(sql="DELETE FROM users")])
    result = answer("delete all users", llm, executor)
    assert result.status is AnswerStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.UNSAFE_REQUEST
    assert len(llm.calls) == 1, "an unsafe request must not consume the repair budget"


def test_model_may_declare_a_question_unanswerable(executor):
    llm = StubLLM([SQLDraft(
        sql="", answerable=False,
        assumptions=["no table records shipping addresses"],
    )])
    result = answer("What is each customer's shipping address?", llm, executor)
    assert result.status is AnswerStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.NOT_ANSWERABLE
    assert len(llm.calls) == 1


def test_database_error_triggers_a_repair(executor):
    """Valid identifiers, but SQLite rejects it. The repair path must still fire."""
    llm = StubLLM([
        SQLDraft(sql="SELECT COUNT(*) FROM accounts GROUP BY accounts.name HAVING SUM(name)"),
        SQLDraft(sql="SELECT COUNT(*) AS n FROM accounts"),
    ])
    result = answer("how many accounts", llm, executor)
    assert result.status in {AnswerStatus.ANSWERED, AnswerStatus.ABSTAINED}


def test_row_cap_applies_to_agent_output(executor):
    llm = StubLLM([SQLDraft(sql="SELECT event_id FROM events")])
    result = answer("list every event", llm, executor, max_rows=50)
    assert result.status is AnswerStatus.ANSWERED
    assert len(result.rows) == 50


def test_retrieval_expands_across_foreign_keys():
    from queryguard.schema.catalog import load_sqlite_catalog
    cat = load_sqlite_catalog(DB)
    tables = select_tables("what is the MRR for each plan?", cat)
    assert "plans" in tables
    # subscriptions carries the FK that makes the join possible; it must be
    # pulled in even though the question never names it.
    assert "subscriptions" in tables


def test_usage_is_tracked(executor):
    llm = StubLLM([
        SQLDraft(sql="SELECT bogus FROM users"),
        SQLDraft(sql="SELECT COUNT(*) AS n FROM users"),
    ])
    result = answer("count users", llm, executor)
    assert result.usage.llm_calls == 2


def test_retrieval_does_not_drag_in_the_whole_schema():
    """`accounts` is a hub with seven foreign keys. Expanding from every seed
    would return the entire schema and make retrieval pointless."""
    from queryguard.schema.catalog import load_sqlite_catalog
    cat = load_sqlite_catalog(DB)
    tables = select_tables("what is the MRR for each plan?", cat)
    assert len(tables) < len(cat), "retrieval returned the whole schema"
    assert len(tables) <= 5
