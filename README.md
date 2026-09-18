# QueryGuard

A natural-language-to-SQL agent that is built to be *wrong safely*. It refuses
what it cannot verify, states the assumptions its answer depends on, and cannot
write to a database even if it tries.

```bash
make install && make db-sqlite
make ask Q="How many accounts churned last quarter?"
```

```
> How many accounts are there?

assumptions:
  - Counted total records in the accounts table to represent the overall number of accounts.

sql:
  SELECT COUNT(*) FROM accounts

  COUNT(*)
  --------
  400

  answered | attempts=1 | llm_calls=1 | 40622ms
```

## The idea

Most text-to-SQL demos optimise for the happy path. The interesting failures are
elsewhere: the model invents a column, or it answers a subtly different question
than the one asked and says nothing about the substitution. QueryGuard treats
both as first-class outcomes.

Three properties, each enforced somewhere specific rather than requested in a
prompt:

| Property | Where it is enforced | Why not the prompt |
|---|---|---|
| Cannot write | DB connection opened read-only; Postgres role holds no write grants | A prompt is a request; a grant is a boundary |
| Cannot reference what does not exist | Static validation against the live schema catalog | The model is confident about columns it invented |
| Must show its reasoning | Structured output with a required `assumptions` field | An unstated assumption is indistinguishable from a correct answer |

## Architecture

A LangGraph state machine. Every node except `draft` is deterministic, which is
what lets the whole pipeline be tested without an API key.

```
retrieve ──► draft ──► validate ──┬──► execute ──► answered
               ▲                  │                   │
               └──────────────────┴───────────────────┘
                     bounded repair (≤3), fed the validator's own message
```

Two design decisions worth a reviewer's attention:

- **An unsafe request skips the repair budget.** A `DELETE` returns
  `UNSAFE_REQUEST` on the first pass. Re-asking cannot make the request safe, so
  spending three repairs on it is waste.
- **Transport failure and semantic failure are handled separately.** LangGraph's
  `RetryPolicy` covers an overloaded model; the repair edges cover wrong SQL.
  Conflating them means a network blip eats the repair budget — and on a
  rate-limited key, retrying a quota refusal actively destroys the remaining
  budget.

## Verified results

Every number below has a command next to it. None are estimates.

### Safety

| Metric | Result | Reproduce |
|---|---|---|
| Write/DDL attempts against the Postgres role | **22 attempted, 0 changed any state** | `make pg-verify` |
| Attack surface covered | INSERT, UPDATE, DELETE, TRUNCATE, DROP, CREATE, CREATE TEMP, SELECT INTO, ALTER, GRANT, COPY TO FILE — each tried twice | `make pg-verify` |
| Write sent *past* the validator | rejected by SQLite: `attempt to write a readonly database` | `make test` |
| Unsafe-SQL classes rejected statically | 6 (`parse_error`, `unknown_table`, `unknown_column`, `write_statement`, `forbidden_construct`, `multiple_statements`) | `make test` |

The Postgres verification runs twice: once normally, and once after the role
disables its own `default_transaction_read_only` — which a role is permitted to
do, so the first pass alone proves little. It asserts on **effects** (row
counts, privileges, table counts), not on whether a statement raised, because a
non-owner `GRANT` returns successfully while changing nothing.

### Correctness harness

| Metric | Result | Reproduce |
|---|---|---|
| Oracle accuracy on BIRD Mini-Dev (50 q) | **100.0%** | `make eval-oracle N=50` |
| Tests | **65 passing** | `make test` |
| Lint | clean | `make lint` |

Oracle mode feeds gold SQL through the real graph in place of a model. It scores
the *harness*, not the agent: if it is not ~100%, the scoring logic is broken and
any accuracy number the project reports is meaningless. It costs nothing to run.

> **Agent accuracy against BIRD is not yet measured.** The harness is built and
> validated, but the Gemini free tier allows 20 requests/day and a meaningful
> run needs several hundred. Rather than publish a number from a 10-question
> sample, this section stays empty until a real run happens. `make eval N=10` is
> resumable and accumulates across days.

### Efficiency

| Metric | Result | Reproduce |
|---|---|---|
| Schema tokens saved by FK-aware retrieval (500 BIRD questions) | **37% mean reduction**; median 3 of 7 tables shown | see *Retrieval* below |
| Static validation latency | median **0.30 ms** | `make test` |
| Aggregate over 106,464 rows | **7 ms** | `make ask Q="how many logins?"` |
| Query timeout | fires at the configured limit, via a SQLite progress handler | `make test` |

### Dataset integrity

The synthetic SaaS database plants seven ambiguities deliberately, so the agent
can be tested on whether it *notices* them. All magnitudes are locked by tests:

| Trap | Naive query error | Reproduce |
|---|---|---|
| Event renamed mid-history (`login` → `user_login`) | undercounts by **36%** (15,440 vs 24,270) | `make test` |
| Soft deletes (`users.deleted_at`) | overcounts by **10%** (797 vs 725, 31-day window) | `make test` |
| Two churn definitions | disagree on **3** accounts (52 vs 55) | `make test` |
| Missing attribution rows | INNER JOIN drops **103 of 400** accounts | `make test` |
| Invited ≠ activated | **486 of 2,660** users never activated | `make test` |
| Money in `*_cents` | 100× error | `make test` |
| Revenue defined two ways | different magnitudes entirely | — |

Regeneration is byte-identical for a given seed (verified by sha256 across
runs), so these numbers are checkable rather than asserted.

## What running a real benchmark found

Pointing the project at BIRD immediately surfaced two defects that the
synthetic dataset never could. Both are fixed, both have regression tests.

1. **Reserved-word table names.** BIRD's `financial` database has a table named
   `order`. The catalog interpolated table names into `PRAGMA table_info(...)`
   unquoted, so introspection died with a syntax error and took the whole
   evaluation run with it.
2. **`EXCEPT` and `INTERSECT` classified as attacks.** They parse to their own
   sqlglot node types, not `exp.Union`, so the validator fell through to
   `FORBIDDEN_CONSTRUCT` — which routes to *unsafe request*. A legitimate
   read-only set operation was being reported as an attempted attack.

The second is the more interesting failure: the system was not merely wrong, it
was wrong in the direction that looks like vigilance.

## Layout

```
src/queryguard/
  agent/       LangGraph state machine, prompts, retrieval, LLM client + cache
  validate/    static checks against the catalog; LIMIT policy
  execution/   read-only sandboxed executors (SQLite, Postgres)
  schema/      catalog introspection, shared across both engines
evaluation/    BIRD scoring harness (execution accuracy, oracle mode)
data/          synthetic dataset generator, read-only role verification
```

## Commands

```bash
make install      # venv + dependencies
make db-sqlite    # build the synthetic database
make db-postgres  # same rows into Postgres, behind a read-only role
make pg-verify    # attack that role 22 ways, assert nothing changed
make bird         # fetch BIRD Mini-Dev (500 questions, 11 databases)
make eval-oracle  # validate the scoring harness — free, no API calls
make eval N=10    # score the real agent (resumable)
make test         # 65 tests, no API key required
make ask Q="..."  # ask a question
```

## Known limitations

- **Agent accuracy is unmeasured.** See above.
- `gemini-3.6-flash` ignores `temperature`, so `QG_TEMPERATURE` no longer
  controls determinism. The on-disk response cache is what makes re-runs
  reproducible.
- Retrieval is lexical, not semantic. When a question shares no vocabulary with
  the schema it falls back to showing every table.
- The synthetic dataset's numbers are not externally verifiable the way BIRD's
  are — both the data and the questions were generated here, which is a real
  bias risk. That is why BIRD is intended to carry the accuracy claim.
- Column resolution skips identifiers it cannot attribute to a base table
  (CTEs, derived tables), so it under-reports rather than blocking valid SQL.
