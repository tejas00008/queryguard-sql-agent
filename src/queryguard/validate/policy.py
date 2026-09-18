"""Query policy applied after validation passes.

Currently one rule: bound the result size in the SQL itself.

The executor already caps rows it reads, but a LIMIT in the query lets the
database stop early instead of materialising a large result set and having the
client walk away from it. "Show me every event" should cost one page of work,
not a full scan.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp


def enforce_limit(sql: str, max_rows: int, dialect: str = "sqlite") -> tuple[str, bool]:
    """Add a LIMIT if the outer query has none. Returns (sql, was_added).

    An existing LIMIT is left alone even if it is larger than max_rows -- the
    executor's row cap is what actually protects memory, and rewriting the
    model's own LIMIT would silently change the answer to a question like
    "the top 500 accounts".
    """
    tree = sqlglot.parse_one(sql, dialect=dialect)
    if tree.find(exp.Limit) is not None:
        return sql, False
    return tree.limit(max_rows).sql(dialect=dialect), True
