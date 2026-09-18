"""Database schema introspection.

The catalog is the ground truth the validator checks generated SQL against and
the source of the text the model sees. Both engines produce the same structure,
so nothing downstream needs to know which database it is talking to.

Identifiers are stored and compared lower-cased. SQL identifiers are
case-insensitive unless quoted, and the model is inconsistent about casing, so
normalising once here avoids scattering .lower() through the validator.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


def _quote(identifier: str) -> str:
    """Quote an identifier for interpolation into SQL.

    Necessary because real schemas use reserved words as table names -- BIRD's
    `financial` database has a table called `order` -- and an unquoted
    PRAGMA table_info(order) is a syntax error. Interpolation is unavoidable
    here: PRAGMA and FROM take an identifier, not a bindable parameter.
    Doubling any embedded quote is what makes that interpolation safe.
    """
    return '"' + identifier.replace('"', '""') + '"'


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False


@dataclass(frozen=True)
class ForeignKey:
    from_table: str
    from_column: str
    to_table: str
    to_column: str


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    row_count: int | None = None

    @property
    def column_names(self) -> set[str]:
        return {c.name for c in self.columns}


@dataclass
class Catalog:
    tables: dict[str, Table] = field(default_factory=dict)
    engine: str = "sqlite"

    # --- lookups (all case-insensitive) ---

    def has_table(self, name: str) -> bool:
        return name.lower() in self.tables

    def get(self, name: str) -> Table | None:
        return self.tables.get(name.lower())

    def has_column(self, table: str, column: str) -> bool:
        t = self.get(table)
        return bool(t and column.lower() in t.column_names)

    def tables_with_column(self, column: str) -> list[str]:
        col = column.lower()
        return [name for name, t in self.tables.items() if col in t.column_names]

    # --- relationships ---

    def neighbours(self, names: set[str]) -> set[str]:
        """Tables one foreign key away from `names`, in either direction.

        Retrieval that returns `orders` without `customers` produces SQL that
        can't join. Expanding along foreign keys costs a few hundred tokens and
        removes a whole class of failure.
        """
        wanted = {n.lower() for n in names}
        found: set[str] = set()
        for name, table in self.tables.items():
            for fk in table.foreign_keys:
                if name in wanted:
                    found.add(fk.to_table)
                if fk.to_table in wanted:
                    found.add(name)
        return found - wanted

    # --- prompt rendering ---

    def to_ddl(self, only: set[str] | None = None, include_counts: bool = False) -> str:
        """Compact CREATE TABLE text for the prompt.

        Not real DDL -- no constraints or defaults, since they cost tokens and
        the model doesn't need them to write a SELECT. Foreign keys stay,
        because they are how it knows what joins to what.
        """
        chosen = sorted(self.tables) if only is None else sorted(
            n for n in self.tables if n in {o.lower() for o in only}
        )
        blocks = []
        for name in chosen:
            table = self.tables[name]
            cols = [f"  {c.name} {c.type}{' PRIMARY KEY' if c.primary_key else ''}"
                    for c in table.columns]
            for fk in table.foreign_keys:
                cols.append(f"  FOREIGN KEY ({fk.from_column}) -> {fk.to_table}({fk.to_column})")
            header = f"CREATE TABLE {name} ("
            if include_counts and table.row_count is not None:
                header = f"-- {table.row_count:,} rows\n" + header
            blocks.append(header + "\n" + ",\n".join(cols) + "\n);")
        return "\n\n".join(blocks)

    def __len__(self) -> int:
        return len(self.tables)


def load_sqlite_catalog(path: str | Path, with_counts: bool = False) -> Catalog:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        catalog = Catalog(engine="sqlite")
        names = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for name in names:
            table = Table(name=name.lower())
            for _cid, col, ctype, notnull, _dflt, pk in conn.execute(
                f"PRAGMA table_info({_quote(name)})"
            ):
                table.columns.append(Column(
                    name=col.lower(), type=(ctype or "TEXT").upper(),
                    nullable=not notnull, primary_key=bool(pk),
                ))
            for row in conn.execute(f"PRAGMA foreign_key_list({_quote(name)})"):
                # (id, seq, table, from, to, on_update, on_delete, match)
                table.foreign_keys.append(ForeignKey(
                    from_table=name.lower(), from_column=str(row[3]).lower(),
                    to_table=str(row[2]).lower(),
                    to_column=str(row[4]).lower() if row[4] else "",
                ))
            if with_counts:
                table.row_count = conn.execute(
                    f"SELECT COUNT(*) FROM {_quote(name)}"
                ).fetchone()[0]
            catalog.tables[table.name] = table
        return catalog
    finally:
        conn.close()


def load_postgres_catalog(dsn: str, with_counts: bool = False) -> Catalog:
    import psycopg

    catalog = Catalog(engine="postgres")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns WHERE table_schema = 'public' "
            "ORDER BY table_name, ordinal_position"
        )
        for tname, cname, ctype, nullable in cur.fetchall():
            table = catalog.tables.setdefault(tname.lower(), Table(name=tname.lower()))
            table.columns.append(Column(
                name=cname.lower(), type=ctype.upper(), nullable=(nullable == "YES"),
            ))

        cur.execute("""
            SELECT tc.table_name, kcu.column_name,
                   ccu.table_name AS foreign_table, ccu.column_name AS foreign_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
        """)
        for tname, cname, ftable, fcolumn in cur.fetchall():
            if t := catalog.get(tname):
                t.foreign_keys.append(ForeignKey(
                    from_table=tname.lower(), from_column=cname.lower(),
                    to_table=ftable.lower(), to_column=fcolumn.lower(),
                ))

        cur.execute("""
            SELECT tc.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'
        """)
        pks = {(t.lower(), c.lower()) for t, c in cur.fetchall()}
        for tname, table in catalog.tables.items():
            table.columns = [
                Column(c.name, c.type, c.nullable, (tname, c.name) in pks)
                for c in table.columns
            ]

        if with_counts:
            for tname, table in catalog.tables.items():
                cur.execute(f"SELECT COUNT(*) FROM {_quote(tname)}")
                table.row_count = cur.fetchone()[0]

    return catalog
