# Data

Two datasets. They serve different purposes and their results are never pooled.

## 1. BIRD (external benchmark) — `data/bird/`

Not committed. Fetched by `scripts/fetch_bird.py` (added in Stage 3).

BIRD is a public text-to-SQL benchmark with gold SQL for every question, which
is what makes my accuracy numbers checkable by someone who doesn't trust me.
I use a seeded, difficulty-stratified sample of the dev set rather than the
full set, because every eval run costs API calls and I re-run them across four
system versions. The sample size and seed are recorded in
`evaluation/datasets/bird_subset.json`.

BIRD ships as SQLite. That is the only engine it runs on here.

## 2. SaaS analytics (synthetic) — `data/db/saas.sqlite` + Postgres

Not committed (13 MB, and regenerating is deterministic). Build it with:

```bash
make db-sqlite                 # SQLite only
make db-postgres               # loads the same rows into Postgres
```

### Why synthetic

I needed a schema whose ambiguities I control. BIRD tells me whether the agent
can write correct SQL; it doesn't tell me whether the agent notices that a
question has two defensible readings. So I built a schema with specific traps
and then wrote questions that step on them.

The cost of this choice: these numbers are not externally verifiable the way
BIRD's are, and I generated both the data and the questions, which is a real
bias risk. That's why BIRD carries the headline accuracy claim and this dataset
carries the ambiguity and cross-engine analysis.

### Schema

9 tables: `plans`, `campaigns`, `accounts`, `account_sources`, `users`,
`subscriptions`, `invoices`, `support_tickets`, `events`.

| Table | Rows |
|---|---|
| plans | 4 |
| campaigns | 12 |
| accounts | 400 |
| account_sources | 297 |
| users | 2,660 |
| subscriptions | 475 |
| invoices | 3,523 |
| support_tickets | 788 |
| events | 106,464 |

Dataset "today" is fixed at **2026-09-18**. Nothing in the data occurs after it.

### The traps (measured, not asserted)

Every number below comes from running both the naive and the correct query
against the generated database. The verification lives in
`tests/test_saas_dataset.py`, so these claims fail loudly if the generator
changes rather than quietly becoming fiction.

"Active" means a `last_seen_at` inside a **31-day** window ending at the fixed
dataset "today". The window matters: the soft-delete numbers below are not
reproducible without it.

| Trap | What a naive query does | Measured error |
|---|---|---|
| **Event rename** — `login` was renamed `user_login` 150 days ago; both names exist in history | Filters `event_name = 'user_login'` | Undercounts logins by **36%** (15,440 vs 24,270) |
| **Soft delete** — `users.deleted_at` | Counts by `last_seen_at` alone | Overcounts active users by **10%** (797 vs 725, over a 31-day window) |
| **Two churn definitions** — `accounts.churned_at` vs `subscriptions.status = 'canceled'` | Picks one silently | The two disagree on **3** accounts (52 vs 55) |
| **Missing attribution** — 26% of accounts are organic with no `account_sources` row | `JOIN account_sources` | Drops **103 of 400** accounts |
| **Invited ≠ activated** — `created_at` vs `activated_at` | Counts `created_at` | **486 of 2,660** users never activated |
| **Money in cents** — every amount is `*_cents` | Reports raw integers | Off by 100× |
| **Revenue means two things** — `subscriptions.mrr_cents` vs paid `invoices.amount_cents` | Picks one silently | Different magnitudes entirely |

The point of these isn't to make the agent fail. It's to check whether the
agent *states which reading it chose*. A query that picks `activated_at` and
says so is correct behaviour. One that picks it silently is not.

### Determinism

Same `--seed` produces a byte-identical SQLite file (verified by sha256 across
regeneration). Default seed `20260918` is set in `.env.example`.

Row counts also matched exactly when the generator was run on a different
machine and CPU architecture (arm64 and x86_64), which means the seeded
generation doesn't depend on platform floating-point behaviour. That matters
because the evaluation questions have gold answers computed against this data.

## Safety verification

`data/scripts/verify_readonly.py` attacks the Postgres role with 11 write and
DDL statements, twice: once in a normal session, and once after the role turns
off its own `default_transaction_read_only` (which a role is permitted to do,
so the first pass alone proves little). It asserts on *effects* -- row counts,
actual privileges, table count, column count -- not on whether a statement
raised, because a non-owner `GRANT` returns successfully while changing nothing.

Current result: **22 attempts, none changed any state.** Run it yourself with
`make pg-verify`.

### Known limitations

- Distributions are simple (uniform or lightly weighted). Real warehouses have
  heavy-tailed account sizes and strong seasonality; this has neither.
- Text fields are drawn from small vocabularies, so string matching is easier
  here than in reality.
- No nulls in places real data has them (missing regions, malformed emails).
- 400 accounts is small enough that query performance is never a factor, so
  this dataset says nothing about the agent's behaviour at warehouse scale.
