"""Prompt construction.

The system prompt does the work that the rest of the system cannot. The
validator can prove SQL is *safe* and that its identifiers *exist*; it cannot
prove the query answers the question that was asked, and it cannot notice that
the question had two defensible readings. Those two jobs belong here, which is
why the prompt spends most of its length on assumptions and answerability
rather than on SQL syntax the model already knows.

The repair prompt is deliberately narrow: it restates the failure and asks for
a corrected query, without re-litigating the question. Handing the model its
own previous SQL plus a precise error is a much easier task than the original
one, and keeping the instructions identical between attempts means a failure
to converge is the model's, not a moving target's.
"""

from __future__ import annotations

SYSTEM = """\
You translate an analytics question into exactly one read-only SQL SELECT \
statement for a {dialect} database.

Rules:
1. Emit exactly ONE statement. It must be a SELECT. Never INSERT, UPDATE, \
DELETE, CREATE, DROP, ALTER, GRANT, TRUNCATE, or PRAGMA.
2. Use only tables and columns that appear in the schema below. Never invent \
an identifier because it "should" exist.
3. List in `tables_used` every table your SQL reads.
4. Record in `assumptions` every interpretation choice you made. This matters \
more than it sounds:
   - If a question says "active users" and the schema has both `last_seen_at` \
and `deleted_at`, say which one you used and why.
   - If money is stored in a `*_cents` column, say whether you converted it.
   - If an event name appears to have been renamed part-way through history, \
say which names you counted.
   - If two tables could both define the metric (e.g. subscription MRR vs \
paid invoices), name the one you chose.
   A query that picks one reading and says so is correct. One that picks \
silently is not.
5. If the schema genuinely cannot answer the question, set `answerable` to \
false and explain why in `assumptions`. Do not return a query that answers a \
different, easier question instead.
6. Prefer explicit JOINs. Use a LEFT JOIN when rows without a match should \
still be counted -- an INNER JOIN silently drops them.

Schema:
{schema}
"""

USER = "Question: {question}"

REPAIR = """\
Your previous SQL was rejected.

SQL you produced:
{sql}

Why it was rejected:
{feedback}

Produce a corrected single read-only SELECT that fixes exactly these problems. \
Keep the parts that were not criticised. If the rejection shows the question \
cannot be answered from this schema, set `answerable` to false instead of \
guessing again.
"""


def system_prompt(schema_ddl: str, dialect: str = "sqlite") -> str:
    return SYSTEM.format(schema=schema_ddl, dialect=dialect)


def user_prompt(question: str) -> str:
    return USER.format(question=question)


def repair_prompt(sql: str, feedback: str) -> str:
    return REPAIR.format(sql=sql, feedback=feedback)
