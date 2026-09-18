"""Command-line entry point.

    python -m queryguard ask "how many accounts churned last quarter?"

Printing the assumptions above the table is deliberate. The system's claim is
not "here is your answer" but "here is your answer, and here is the reading of
your question it depends on" -- burying that below the numbers would defeat
the point of asking the model for it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from queryguard.agent.graph import answer
from queryguard.agent.llm import build_llm
from queryguard.config import get_settings
from queryguard.execution.sandbox import PostgresExecutor, SqliteExecutor
from queryguard.models import AnswerResult, AnswerStatus


def _executor(engine: str, db_id: str, settings):
    if engine == "postgres":
        if not settings.pg_dsn:
            sys.exit("QG_PG_DSN is not set; run `make db-postgres` first.")
        return PostgresExecutor(settings.pg_dsn, settings.max_rows, settings.query_timeout_s)
    return SqliteExecutor(settings.sqlite_path(db_id), settings.max_rows,
                          settings.query_timeout_s)


def _render(result: AnswerResult, show_sql: bool) -> None:
    if result.status is AnswerStatus.ABSTAINED:
        print(f"\n  abstained ({result.abstain_reason})")
        if result.error:
            print(f"  {result.error}")
        for a in result.assumptions:
            print(f"  - {a}")
    if result.assumptions and result.status is AnswerStatus.ANSWERED:
        print("\nassumptions:")
        for a in result.assumptions:
            print(f"  - {a}")
    if show_sql and result.sql:
        print(f"\nsql:\n  {result.sql}")
    if result.status is AnswerStatus.ANSWERED:
        print()
        widths = [
            max(len(str(c)), *(len(str(r[i])) for r in result.rows)) if result.rows
            else len(str(c))
            for i, c in enumerate(result.columns)
        ]
        print("  " + "  ".join(str(c).ljust(w) for c, w in zip(result.columns, widths)))
        print("  " + "  ".join("-" * w for w in widths))
        for row in result.rows[:50]:
            print("  " + "  ".join(str(v).ljust(w) for v, w in zip(row, widths)))
        if len(result.rows) > 50:
            print(f"  ... {len(result.rows) - 50} more rows")
    print(
        f"\n  {result.status} | attempts={result.attempts} "
        f"| tables={','.join(result.retrieved_tables) or '-'} "
        f"| llm_calls={result.usage.llm_calls} cached={result.usage.cached_calls} "
        f"| {result.latency_ms:.0f}ms"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="queryguard")
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="answer a question against a database")
    ask.add_argument("question")
    ask.add_argument("--engine", choices=["sqlite", "postgres"], default="sqlite")
    ask.add_argument("--db-id", default="saas")
    ask.add_argument("--show-sql", action="store_true")
    ask.add_argument("--no-cache", action="store_true", help="bypass the LLM cache")

    schema = sub.add_parser("schema", help="print the schema the model would see")
    schema.add_argument("--engine", choices=["sqlite", "postgres"], default="sqlite")
    schema.add_argument("--db-id", default="saas")
    schema.add_argument("--question", help="show only the tables retrieved for this question")

    args = parser.parse_args(argv)
    settings = get_settings()
    executor = _executor(args.engine, args.db_id, settings)

    if args.command == "schema":
        catalog = executor.catalog()
        only = None
        if args.question:
            from queryguard.agent.retrieval import select_tables
            only = set(select_tables(args.question, catalog))
            print(f"-- retrieved {len(only)} of {len(catalog)} tables\n")
        print(catalog.to_ddl(only=only, include_counts=True))
        return 0

    if args.no_cache:
        settings = settings.model_copy(update={"llm_cache": False})
    try:
        llm = build_llm(settings, cache_dir=Path(".cache/llm"))
    except RuntimeError as exc:
        sys.exit(str(exc))

    print(f"\n> {args.question}")
    result = answer(args.question, llm, executor, db_id=args.db_id,
                    max_repairs=settings.max_repair_attempts, max_rows=settings.max_rows)
    _render(result, show_sql=args.show_sql)
    return 0 if result.status is AnswerStatus.ANSWERED else 1


if __name__ == "__main__":
    raise SystemExit(main())
