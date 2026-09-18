"""Shared data contracts.

These types are the seams between the agent's nodes. Keeping them in one
module means the graph, the validator, the executor and the eval harness all
agree on what a "draft" or a "result" is, and a change to any contract breaks
loudly at import time rather than quietly at runtime.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    ABSTAINED = "abstained"
    FAILED = "failed"


class AbstainReason(StrEnum):
    NOT_ANSWERABLE = "not_answerable"      # schema genuinely lacks the data
    REPAIR_EXHAUSTED = "repair_exhausted"  # ran out of attempts
    UNSAFE_REQUEST = "unsafe_request"      # asked for a write / DDL
    AMBIGUOUS = "ambiguous"                # question has >1 defensible reading


class IssueCode(StrEnum):
    PARSE_ERROR = "parse_error"
    UNKNOWN_TABLE = "unknown_table"
    UNKNOWN_COLUMN = "unknown_column"
    WRITE_STATEMENT = "write_statement"
    FORBIDDEN_CONSTRUCT = "forbidden_construct"
    MULTIPLE_STATEMENTS = "multiple_statements"


class SQLDraft(BaseModel):
    """What the LLM is asked to produce. Deliberately more than just a string:
    forcing the model to name its tables and state its assumptions gives the
    validator something to check and gives the user something to disagree with.
    """

    sql: str = Field(description="A single read-only SQL SELECT statement.")
    tables_used: list[str] = Field(
        default_factory=list,
        description="Tables the query reads, as the model believes it used them.",
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Interpretation choices made, e.g. which column defines 'active'.",
    )
    answerable: bool = Field(
        default=True,
        description="False if the question cannot be answered from the given schema.",
    )


class ValidationIssue(BaseModel):
    code: IssueCode
    message: str
    identifier: str | None = None

    def as_feedback(self) -> str:
        """The string handed back to the model on a repair attempt."""
        return f"[{self.code}] {self.message}"


class ValidationVerdict(BaseModel):
    ok: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    normalized_sql: str | None = None

    @property
    def feedback(self) -> str:
        return "\n".join(i.as_feedback() for i in self.issues)


class ExecutionResult(BaseModel):
    ok: bool
    columns: list[str] = Field(default_factory=list)
    rows: list[tuple[Any, ...]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    duration_ms: float = 0.0
    error: str | None = None
    engine: str = "sqlite"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    cached_calls: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            llm_calls=self.llm_calls + other.llm_calls,
            cached_calls=self.cached_calls + other.cached_calls,
        )


class AnswerResult(BaseModel):
    """The single thing the API and the eval harness both consume."""

    question: str
    db_id: str
    status: AnswerStatus
    sql: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[tuple[Any, ...]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    abstain_reason: AbstainReason | None = None
    error: str | None = None

    # --- how we got here ---
    version: str = "v0"
    attempts: int = 0
    retrieved_tables: list[str] = Field(default_factory=list)
    sql_history: list[str] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    latency_ms: float = 0.0
    trace_id: str | None = None
