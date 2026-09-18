"""The agent, as a LangGraph state machine.

    retrieve -> draft -> validate -+-> execute -+-> END
                  ^                |            |
                  +----------------+------------+
                        (bounded repair)

Why a graph rather than a while-loop: the repair cycle has two *different*
entry points -- a query rejected before execution, and a query that parsed,
validated, and then failed in the database -- and both feed the same drafting
node with different feedback. Expressing that as edges keeps the retry budget
in one place instead of duplicating it in two `if attempts <` branches, and it
makes the trace the eval harness reads fall out of the state rather than having
to be assembled by hand.

The budget is `max_repair_attempts` *repairs* on top of the first draft, so the
model is called at most 1 + N times per question.
"""

from __future__ import annotations

import time
import uuid

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from queryguard.agent.llm import DraftLLM
from queryguard.agent.prompts import repair_prompt, system_prompt, user_prompt
from queryguard.agent.retrieval import select_tables
from queryguard.agent.state import GraphState
from queryguard.execution.sandbox import Executor
from queryguard.models import (
    AbstainReason,
    AnswerResult,
    AnswerStatus,
    IssueCode,
    SQLDraft,
    Usage,
)
from queryguard.validate.policy import enforce_limit
from queryguard.validate.static_checks import validate as static_validate


def _is_transient(exc: Exception) -> bool:
    """Retry an overloaded model; never retry a quota refusal.

    A 429 means the budget is gone -- on the Gemini free tier that budget is 20
    requests per *day*, so retrying it four times does not recover the request,
    it just spends four more of whatever is left. Only 503/504-class overload,
    where the same request genuinely may succeed moments later, is worth a
    second attempt.
    """
    name = type(exc).__name__
    if "RateLimit" in name or "ResourceExhausted" in name:
        return False
    text = str(exc)
    return "RESOURCE_EXHAUSTED" not in text and "429" not in text


# Issues that mean the *request* was unsafe, not that the model slipped. Asking
# again cannot help, so these skip the repair budget entirely.
_UNSAFE = {IssueCode.WRITE_STATEMENT, IssueCode.FORBIDDEN_CONSTRUCT}


def build_graph(llm: DraftLLM, executor: Executor, max_repairs: int = 3, max_rows: int = 200):
    catalog = executor.catalog()
    dialect = "postgres" if executor.engine == "postgres" else "sqlite"

    def retrieve(state: GraphState) -> GraphState:
        tables = select_tables(state["question"], catalog)
        return {
            "retrieved_tables": tables,
            "schema_ddl": catalog.to_ddl(only=set(tables), include_counts=True),
            "attempts": 0,
            "sql_history": [],
            "issues": [],
            "usage": Usage(),
        }

    def draft(state: GraphState) -> GraphState:
        system = system_prompt(state["schema_ddl"], dialect)
        feedback = state.get("feedback")
        prior = state.get("draft")
        user = (
            repair_prompt(prior.sql if prior else "", feedback)
            if feedback
            else user_prompt(state["question"])
        )
        sql_draft, usage = llm.draft(system, user)
        history = list(state.get("sql_history", []))
        if sql_draft.sql:
            history.append(sql_draft.sql)
        return {
            "draft": sql_draft,
            "attempts": state.get("attempts", 0) + 1,
            "sql_history": history,
            "usage": state.get("usage", Usage()) + usage,
            "feedback": None,
        }

    def validate_node(state: GraphState) -> GraphState:
        sql_draft: SQLDraft = state["draft"]
        verdict = static_validate(sql_draft.sql, catalog, dialect=dialect)
        issues = list(state.get("issues", [])) + list(verdict.issues)
        out: GraphState = {"verdict": verdict, "issues": issues}
        if not verdict.ok:
            out["feedback"] = verdict.feedback
        return out

    def execute_node(state: GraphState) -> GraphState:
        verdict = state["verdict"]
        sql, _ = enforce_limit(verdict.normalized_sql, max_rows, dialect=dialect)
        result = executor.execute(sql)
        out: GraphState = {"execution": result}
        if not result.ok:
            out["feedback"] = f"The database rejected the query: {result.error}"
        return out

    # --- edges ---

    def after_draft(state: GraphState) -> str:
        return "abstain" if not state["draft"].answerable else "validate"

    def after_validate(state: GraphState) -> str:
        verdict = state["verdict"]
        if verdict.ok:
            return "execute"
        if any(i.code in _UNSAFE for i in verdict.issues):
            return "unsafe"
        return "draft" if state["attempts"] <= max_repairs else "exhausted"

    def after_execute(state: GraphState) -> str:
        if state["execution"].ok:
            return END
        return "draft" if state["attempts"] <= max_repairs else "exhausted"

    def abstain(state: GraphState) -> GraphState:
        return {"status": AnswerStatus.ABSTAINED,
                "abstain_reason": AbstainReason.NOT_ANSWERABLE}

    def unsafe(state: GraphState) -> GraphState:
        return {"status": AnswerStatus.ABSTAINED,
                "abstain_reason": AbstainReason.UNSAFE_REQUEST}

    def exhausted(state: GraphState) -> GraphState:
        execution = state.get("execution")
        return {
            "status": AnswerStatus.ABSTAINED,
            "abstain_reason": AbstainReason.REPAIR_EXHAUSTED,
            "error": (execution.error if execution and not execution.ok
                      else state.get("feedback")),
        }

    g = StateGraph(GraphState)
    g.add_node("retrieve", retrieve)
    # Two different kinds of failure, handled in two different places.
    # RetryPolicy covers *transport* failure -- a 503 from an overloaded model,
    # a dropped connection -- where the same request will probably succeed
    # shortly. The repair edges below cover *semantic* failure, where the model
    # answered fine but the SQL was wrong and asking again unchanged would
    # return the same wrong SQL. Conflating them wastes the repair budget on
    # network blips.
    g.add_node("draft", draft, retry_policy=RetryPolicy(max_attempts=3, retry_on=_is_transient))
    g.add_node("validate", validate_node)
    g.add_node("execute", execute_node)
    g.add_node("abstain", abstain)
    g.add_node("unsafe", unsafe)
    g.add_node("exhausted", exhausted)

    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "draft")
    g.add_conditional_edges("draft", after_draft,
                            {"validate": "validate", "abstain": "abstain"})
    g.add_conditional_edges("validate", after_validate,
                            {"execute": "execute", "draft": "draft",
                             "unsafe": "unsafe", "exhausted": "exhausted"})
    g.add_conditional_edges("execute", after_execute,
                            {END: END, "draft": "draft", "exhausted": "exhausted"})
    g.add_edge("abstain", END)
    g.add_edge("unsafe", END)
    g.add_edge("exhausted", END)
    return g.compile()


def answer(question: str, llm: DraftLLM, executor: Executor, db_id: str = "saas",
           max_repairs: int = 3, max_rows: int = 200) -> AnswerResult:
    """Run one question to an AnswerResult -- the contract the CLI and the eval
    harness both consume."""
    started = time.perf_counter()
    graph = build_graph(llm, executor, max_repairs=max_repairs, max_rows=max_rows)
    final = graph.invoke({"question": question, "db_id": db_id})

    execution = final.get("execution")
    draft = final.get("draft")
    status = final.get("status")
    if status is None:
        status = (AnswerStatus.ANSWERED if execution and execution.ok
                  else AnswerStatus.FAILED)

    return AnswerResult(
        question=question,
        db_id=db_id,
        status=status,
        sql=(final.get("verdict").normalized_sql if final.get("verdict") else None)
            or (draft.sql if draft else None),
        columns=execution.columns if execution and execution.ok else [],
        rows=execution.rows if execution and execution.ok else [],
        assumptions=draft.assumptions if draft else [],
        abstain_reason=final.get("abstain_reason"),
        error=final.get("error"),
        attempts=final.get("attempts", 0),
        retrieved_tables=final.get("retrieved_tables", []),
        sql_history=final.get("sql_history", []),
        issues=final.get("issues", []),
        usage=final.get("usage", Usage()),
        latency_ms=(time.perf_counter() - started) * 1000,
        trace_id=uuid.uuid4().hex[:12],
    )
