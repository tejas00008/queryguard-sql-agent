"""Static validation of generated SQL, before anything touches a database.

Two jobs:

1. Refuse anything that isn't a single read-only SELECT. The database role is
   the real security boundary (see data/scripts/verify_readonly.py); this is
   the layer that catches it early and cheaply, and keeps a hostile statement
   out of the query log entirely.

2. Catch references to tables and columns that don't exist. This is the common
   failure -- the model invents `users.is_active` because the schema it was
   shown was too big to read carefully. Catching it here means the repair loop
   gets a precise message ("unknown column is_active on users; users has:
   activated_at, deleted_at, last_seen_at") instead of the database's terser
   one, and it costs no query execution.

On column resolution: sqlglot ships an optimizer (`qualify`) that does this
more thoroughly than I do. I resolve identifiers by hand because the error
message is the product here -- it is fed straight back to the model on a repair
attempt -- and I wanted to control its wording and include near-miss
suggestions. The trade-off is that my resolver is deliberately conservative
around CTEs and derived tables: it skips columns it cannot attribute to a real
base table rather than guessing, so it under-reports rather than blocking valid
SQL. False positives here would be expensive; a false negative just falls
through to the database, which will produce its own error.
"""

from __future__ import annotations

import difflib

import sqlglot
from sqlglot import exp

from queryguard.models import IssueCode, ValidationIssue, ValidationVerdict
from queryguard.schema.catalog import Catalog

# Statement types that must never reach a database.
FORBIDDEN_NODES: dict[type[exp.Expression], str] = {
    exp.Insert: "INSERT", exp.Update: "UPDATE", exp.Delete: "DELETE",
    exp.Drop: "DROP", exp.Create: "CREATE", exp.Alter: "ALTER",
    exp.Grant: "GRANT", exp.TruncateTable: "TRUNCATE",
}

# EXCEPT and INTERSECT parse to their own node types, not to exp.Union. Older
# sqlglot releases lack the shared SetOperation base, so fall back to naming
# them. Missing these rejected valid read-only SQL as a FORBIDDEN_CONSTRUCT --
# and because that code routes to "unsafe request", a legitimate set operation
# was reported as an attempted attack.
_SET_OPS: tuple[type[exp.Expression], ...] = (
    (exp.SetOperation,) if hasattr(exp, "SetOperation")
    else (exp.Union, exp.Except, exp.Intersect)
)
READ_ONLY_ROOTS: tuple[type[exp.Expression], ...] = (
    exp.Select, exp.Subquery, exp.With, *_SET_OPS,
)

# PRAGMA, ATTACH, VACUUM, SET and friends land here -- sqlglot parses anything
# it doesn't model as a Command node, so this catches the long tail.
ALLOWED_COMMANDS: set[str] = set()


def _suggest(name: str, options: set[str]) -> str:
    match = difflib.get_close_matches(name, sorted(options), n=1, cutoff=0.6)
    return f" (did you mean {match[0]}?)" if match else ""


def _collect_real_tables(tree: exp.Expression) -> tuple[dict[str, str], set[str]]:
    """Map every table reference and alias to its base table name.

    Returns (alias -> table_name, opaque_names), where opaque_names covers CTEs
    and derived-table aliases -- names that are real in the query but whose
    columns this validator cannot resolve. A column qualified by one of them is
    left alone rather than reported as unknown.

    The derived-table case is not theoretical: `FROM (SELECT ...) t` produces no
    exp.Table node for `t`, so an earlier version of this function reported
    `t.n` as an unknown table and would have rejected valid SQL.
    """
    opaque = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    opaque |= {
        sub.alias.lower() for sub in tree.find_all(exp.Subquery) if sub.alias
    }
    scope: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if name in opaque:
            continue
        scope[name] = name
        if table.alias:
            scope[table.alias.lower()] = name
    return scope, opaque


def _has_opaque_scope(tree: exp.Expression) -> bool:
    """True if the query contains a CTE or derived table.

    Columns can then legitimately come from a projection this validator can't
    see, so unqualified column checking is switched off. Qualified columns
    pointing at real base tables are still checked.
    """
    if any(tree.find_all(exp.CTE)):
        return True
    return any(
        isinstance(sub.parent, exp.Subquery | exp.From)
        for sub in tree.find_all(exp.Select)
        if sub is not tree
    )


def validate(sql: str, catalog: Catalog, dialect: str = "sqlite") -> ValidationVerdict:
    issues: list[ValidationIssue] = []

    # --- parse ---
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    except sqlglot.ParseError as exc:
        return ValidationVerdict(ok=False, issues=[ValidationIssue(
            code=IssueCode.PARSE_ERROR, message=str(exc).splitlines()[0])])

    if not statements:
        return ValidationVerdict(ok=False, issues=[ValidationIssue(
            code=IssueCode.PARSE_ERROR, message="no SQL statement found")])

    if len(statements) > 1:
        return ValidationVerdict(ok=False, issues=[ValidationIssue(
            code=IssueCode.MULTIPLE_STATEMENTS,
            message=f"expected exactly one statement, found {len(statements)}")])

    tree = statements[0]

    # --- statement type ---
    for node_type, label in FORBIDDEN_NODES.items():
        if isinstance(tree, node_type) or tree.find(node_type):
            issues.append(ValidationIssue(
                code=IssueCode.WRITE_STATEMENT,
                message=f"{label} is not permitted; this system only runs SELECT queries",
                identifier=label))

    for command in tree.find_all(exp.Command):
        name = (command.this or "").upper()
        if name not in ALLOWED_COMMANDS:
            issues.append(ValidationIssue(
                code=IssueCode.FORBIDDEN_CONSTRUCT,
                message=f"{name} is not permitted; this system only runs SELECT queries",
                identifier=name))

    if tree.find(exp.Into):
        issues.append(ValidationIssue(
            code=IssueCode.FORBIDDEN_CONSTRUCT,
            message="SELECT ... INTO writes a new table and is not permitted"))

    if not isinstance(tree, READ_ONLY_ROOTS) and not issues:
        issues.append(ValidationIssue(
            code=IssueCode.FORBIDDEN_CONSTRUCT,
            message=f"expected a SELECT statement, got {type(tree).__name__.upper()}"))

    if issues:
        return ValidationVerdict(ok=False, issues=issues)

    # --- identifiers ---
    scope, opaque_names = _collect_real_tables(tree)
    for name in sorted(set(scope.values())):
        if not catalog.has_table(name):
            issues.append(ValidationIssue(
                code=IssueCode.UNKNOWN_TABLE,
                message=f"unknown table '{name}'{_suggest(name, set(catalog.tables))}",
                identifier=name))

    known_tables = {n for n in set(scope.values()) if catalog.has_table(n)}
    opaque = _has_opaque_scope(tree)
    # Aliases defined in the SELECT list are referenceable from ORDER BY/HAVING.
    output_aliases = {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}

    for column in tree.find_all(exp.Column):
        col = column.name.lower()
        qualifier = (column.table or "").lower()

        if qualifier:
            if qualifier in opaque_names:
                continue
            base = scope.get(qualifier)
            if base is None:
                issues.append(ValidationIssue(
                    code=IssueCode.UNKNOWN_TABLE,
                    message=f"'{qualifier}' in '{qualifier}.{col}' is not a table or alias "
                            f"in this query{_suggest(qualifier, set(scope))}",
                    identifier=qualifier))
            elif catalog.has_table(base) and not catalog.has_column(base, col):
                table = catalog.get(base)
                assert table is not None
                issues.append(ValidationIssue(
                    code=IssueCode.UNKNOWN_COLUMN,
                    message=f"unknown column '{col}' on table '{base}'"
                            f"{_suggest(col, table.column_names)}",
                    identifier=f"{base}.{col}"))
        elif not opaque and col not in output_aliases and known_tables:
            if not any(catalog.has_column(t, col) for t in known_tables):
                everything = {c for t in known_tables
                              for c in (catalog.get(t).column_names if catalog.get(t) else set())}
                issues.append(ValidationIssue(
                    code=IssueCode.UNKNOWN_COLUMN,
                    message=f"unknown column '{col}'; not present in "
                            f"{', '.join(sorted(known_tables))}{_suggest(col, everything)}",
                    identifier=col))

    # Deduplicate: the same bad identifier often appears several times.
    seen: set[tuple[str, str | None]] = set()
    unique: list[ValidationIssue] = []
    for issue in issues:
        key = (issue.code, issue.identifier)
        if key not in seen:
            seen.add(key)
            unique.append(issue)

    return ValidationVerdict(
        ok=not unique, issues=unique,
        normalized_sql=tree.sql(dialect=dialect) if not unique else None,
    )
