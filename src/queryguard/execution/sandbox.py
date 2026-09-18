"""Sandboxed query execution.

Three limits apply to every query, regardless of what the model produced:

  read-only   -- SQLite opens with mode=ro; Postgres connects as a role with no
                 write grants. Neither is negotiable from the SQL side.
  timeout     -- an unbounded scan on a large table hangs the request. SQLite
                 gets an interrupt via a progress handler, Postgres a
                 server-side statement_timeout (which matters: an application
                 timeout leaves the query burning CPU on the server).
  row cap     -- a question like "list every event" would otherwise pull 100k
                 rows into memory to answer a question nobody wanted answered.

The executors do not inspect SQL. Deciding what is safe to run is the
validator's job; this layer assumes the SQL is hostile and contains it anyway.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Protocol

from queryguard.models import ExecutionResult
from queryguard.schema.catalog import Catalog, load_postgres_catalog, load_sqlite_catalog


class Executor(Protocol):
    engine: str

    def execute(self, sql: str) -> ExecutionResult: ...
    def catalog(self) -> Catalog: ...


class SqliteExecutor:
    engine = "sqlite"

    def __init__(self, path: str | Path, max_rows: int = 200, timeout_s: float = 10.0):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"database not found: {self.path} (run `make db-sqlite`)")
        self.max_rows = max_rows
        self.timeout_s = timeout_s
        self._catalog: Catalog | None = None

    def catalog(self) -> Catalog:
        if self._catalog is None:
            self._catalog = load_sqlite_catalog(self.path, with_counts=True)
        return self._catalog

    def execute(self, sql: str) -> ExecutionResult:
        started = time.perf_counter()
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        deadline = started + self.timeout_s

        # Called every N bytecode instructions; a non-zero return aborts the
        # query. This is the only way to bound a long-running SQLite statement.
        def watchdog() -> int:
            return 1 if time.perf_counter() > deadline else 0

        conn.set_progress_handler(watchdog, 10_000)
        try:
            cur = conn.execute(sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(self.max_rows + 1)
            truncated = len(rows) > self.max_rows
            rows = rows[: self.max_rows]
            return ExecutionResult(
                ok=True, columns=columns, rows=[tuple(r) for r in rows],
                row_count=len(rows), truncated=truncated,
                duration_ms=(time.perf_counter() - started) * 1000, engine=self.engine,
            )
        except sqlite3.Error as exc:
            elapsed = time.perf_counter() - started
            message = str(exc)
            if "interrupted" in message.lower():
                message = f"query exceeded the {self.timeout_s:.0f}s time limit"
            return ExecutionResult(
                ok=False, error=message, duration_ms=elapsed * 1000, engine=self.engine,
            )
        finally:
            conn.set_progress_handler(None, 0)
            conn.close()


class PostgresExecutor:
    engine = "postgres"

    def __init__(self, dsn: str, max_rows: int = 200, timeout_s: float = 10.0):
        if "queryguard_ro" not in dsn:
            # Cheap guard against a misconfigured .env pointing at the admin role.
            raise ValueError("refusing to execute as anything but the read-only role")
        self.dsn = dsn
        self.max_rows = max_rows
        self.timeout_s = timeout_s
        self._catalog: Catalog | None = None

    def catalog(self) -> Catalog:
        if self._catalog is None:
            self._catalog = load_postgres_catalog(self.dsn, with_counts=True)
        return self._catalog

    def execute(self, sql: str) -> ExecutionResult:
        import psycopg

        started = time.perf_counter()
        try:
            with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {int(self.timeout_s * 1000)}")
                cur.execute(sql)
                columns = [d.name for d in cur.description] if cur.description else []
                rows = cur.fetchmany(self.max_rows + 1)
                truncated = len(rows) > self.max_rows
                rows = rows[: self.max_rows]
                return ExecutionResult(
                    ok=True, columns=columns, rows=[tuple(r) for r in rows],
                    row_count=len(rows), truncated=truncated,
                    duration_ms=(time.perf_counter() - started) * 1000, engine=self.engine,
                )
        except psycopg.Error as exc:
            # Postgres error text is richer than SQLite's and often names the
            # offending identifier plus a suggestion. The repair loop consumes
            # this directly, which is why the cross-engine comparison is
            # interesting -- see experiments/.
            return ExecutionResult(
                ok=False, error=str(exc).strip(),
                duration_ms=(time.perf_counter() - started) * 1000, engine=self.engine,
            )
