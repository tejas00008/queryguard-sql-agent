"""The object that flows between graph nodes.

LangGraph merges what a node returns into this dict, so every node stays a
pure `state -> partial state` function. Keeping the accumulating fields
(`sql_history`, `issues`, `usage`) in the state rather than in a node closure
is what makes a run inspectable after the fact: the eval harness reads the
same trace the CLI prints.
"""

from __future__ import annotations

from typing import TypedDict

from queryguard.models import (
    AbstainReason,
    AnswerStatus,
    ExecutionResult,
    SQLDraft,
    Usage,
    ValidationIssue,
    ValidationVerdict,
)


class GraphState(TypedDict, total=False):
    # --- inputs, set once ---
    question: str
    db_id: str
    dialect: str

    # --- retrieval ---
    retrieved_tables: list[str]
    schema_ddl: str

    # --- the loop ---
    draft: SQLDraft | None
    verdict: ValidationVerdict | None
    execution: ExecutionResult | None
    attempts: int
    feedback: str | None       # what the model is told on a repair pass

    # --- accumulated across attempts ---
    sql_history: list[str]
    issues: list[ValidationIssue]
    usage: Usage

    # --- terminal ---
    status: AnswerStatus
    abstain_reason: AbstainReason | None
    error: str | None
