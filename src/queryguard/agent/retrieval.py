"""Choosing which tables the model gets to see.

Showing the whole schema is fine for nine tables and impossible for BIRD, where
some databases carry dozens. It also actively hurts: a large schema is where
hallucinated columns come from, because the model stops reading carefully.

The scorer is lexical on purpose -- no embeddings, no extra API call. Table and
column names in a warehouse are already English words, and the question is
written in the same vocabulary. When that assumption breaks the FK expansion
catches it, and when *that* fails the validator reports an unknown table and
the repair loop gets another pass with a wider schema.
"""

from __future__ import annotations

import re

from queryguard.schema.catalog import Catalog

_WORD = re.compile(r"[a-z][a-z0-9]*")
# Words that match many tables and discriminate between none of them.
_STOP = {
    "the", "a", "an", "of", "in", "on", "for", "by", "to", "and", "or", "is",
    "are", "was", "were", "how", "many", "much", "what", "which", "who", "when",
    "show", "list", "count", "total", "number", "average", "avg", "sum", "per",
    "each", "all", "with", "without", "that", "this", "from", "have", "has",
    "id", "name", "date", "time", "at", "did", "do", "does", "most", "least",
}


def _tokens(text: str) -> set[str]:
    words = {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}
    # "subscriptions" should match a question that says "subscription".
    return words | {w.rstrip("s") for w in words}


NAME_WEIGHT = 3


def _score(question_tokens: set[str], catalog: Catalog, table: str) -> int:
    """Weight a table-name match above a column-name match.

    Without the weighting, "how many accounts are there?" scores every table
    that merely carries an `account_id` foreign key -- which in this schema is
    seven of nine. The word in the question names the *table*; the fact that
    six other tables point at it is not evidence they are relevant.
    """
    t = catalog.get(table)
    if t is None:
        return 0
    name_hits = len(question_tokens & _tokens(table.replace("_", " ")))
    col_hits = len(question_tokens & _tokens(
        " ".join(c.name.replace("_", " ") for c in t.columns)
    ))
    return NAME_WEIGHT * name_hits + col_hits


def select_tables(question: str, catalog: Catalog, top_k: int = 4,
                  max_expand: int = 3) -> list[str]:
    """Lexically score tables against the question, then expand along foreign keys.

    The expansion is the important half -- a question about "MRR per plan"
    scores `plans` but the join runs through `subscriptions`, which the question
    never names. But expanding from *every* seed is what makes retrieval
    useless: `accounts` is a hub with seven foreign keys, so including it drags
    in the entire schema and we are back to showing the model everything.

    So expansion runs only from the top-scoring tier. The strongest lexical
    signal decides which joins are worth opening up; weaker seeds come along as
    themselves but do not get to pull their own neighbourhoods in.
    """
    q = _tokens(question)
    scored = sorted(
        ((_score(q, catalog, name), name) for name in catalog.tables),
        key=lambda pair: (-pair[0], pair[1]),
    )
    if not scored or scored[0][0] == 0:
        return sorted(catalog.tables)
    # Keep only seeds within half the best score. A table matched by its own
    # name is real evidence; one matched by a single shared column usually isn't.
    floor = max(1, scored[0][0] / 2)
    hits = [(score, name) for score, name in scored if score >= floor][:top_k]
    if not hits:
        # Nothing matched; show everything rather than guessing wrong.
        return sorted(catalog.tables)

    seeds = {name for _, name in hits}
    best = hits[0][0]
    extra = catalog.neighbours({name for score, name in hits if score == best}) - seeds

    # Cap the expansion rather than the total. A hub table like `accounts` has
    # seven foreign keys, and "how many accounts are there?" needs none of them;
    # pulling all seven back in would undo the retrieval entirely. Under-
    # retrieving is the cheaper mistake -- the validator names the missing table
    # and the repair loop gets another pass.
    ranked = {name: score for score, name in scored}
    chosen = seeds | set(sorted(extra, key=lambda n: (-ranked.get(n, 0), n))[:max_expand])
    return sorted(chosen)
