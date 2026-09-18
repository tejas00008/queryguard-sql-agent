"""Score the agent on BIRD Mini-Dev.

The metric is execution accuracy (EX), BIRD's own: run the predicted SQL and
the gold SQL against the same database and compare the result sets. Comparing
SQL text would punish a correct query for phrasing a join differently, which is
not what anyone means by "correct".

Row order is ignored unless the gold query has an ORDER BY. That mirrors SQL's
own semantics -- an unordered SELECT makes no promise about order, so requiring
the agent to match an arbitrary one would measure luck.

Two modes:

  --oracle   feeds the gold SQL to the graph in place of a model. Costs nothing
             and calls nothing. It scores the *harness*, not the agent: if
             oracle accuracy is not ~100%, the comparison logic is broken and
             any real number printed by this file is meaningless.
  (default)  runs the real model.

Because the free Gemini tier allows very few requests per day, --limit is
expected to be small and the run is resumable: results already present in the
output file are not re-run.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from queryguard.agent.graph import answer
from queryguard.agent.llm import StubLLM, build_llm
from queryguard.config import get_settings
from queryguard.execution.sandbox import SqliteExecutor
from queryguard.models import AnswerStatus, SQLDraft

REPO_ROOT = Path(__file__).resolve().parents[1]
BIRD = REPO_ROOT / "data" / "bird"

# Deliberately far above the CLI's 200. The row cap is a safety feature for an
# interactive user; applying it here would mark a correct 500-row answer wrong.
# Some BIRD gold queries legitimately return tens of thousands of rows -- one
# returns 25,061 -- so anything at or above this cap is reported as unscorable
# rather than scored against a truncated result, which would count a correct
# answer as wrong and quietly depress the headline number.
EVAL_MAX_ROWS = 100_000


def db_path(db_id: str) -> Path:
    return BIRD / "databases" / db_id / f"{db_id}.sqlite"


def load_questions(dialect: str = "sqlite") -> list[dict[str, Any]]:
    path = BIRD / "questions" / f"mini_dev_{dialect}.json"
    if not path.exists():
        raise SystemExit(f"{path} not found -- run `make bird` first")
    return json.loads(path.read_text())


def stratified_sample(questions: list[dict], n: int, seed: int) -> list[dict]:
    """Sample proportionally across difficulty, deterministically.

    A flat random sample of 15 from a set that is 20% challenging can easily
    contain zero challenging questions, which would make the score look good
    for the wrong reason.
    """
    if n >= len(questions):
        return list(questions)
    rng = random.Random(seed)
    buckets: dict[str, list[dict]] = {}
    for q in questions:
        buckets.setdefault(q.get("difficulty", "unknown"), []).append(q)
    chosen: list[dict] = []
    for name in sorted(buckets):
        bucket = sorted(buckets[name], key=lambda q: q["question_id"])
        take = max(1, round(n * len(bucket) / len(questions)))
        chosen.extend(rng.sample(bucket, min(take, len(bucket))))
    rng.shuffle(chosen)
    return chosen[:n]


def run_gold(sql: str, path: Path) -> tuple[list[tuple], bool]:
    """Execute gold SQL directly. Read-only, and errors are reported, not raised:
    a handful of BIRD gold queries fail on some SQLite builds, and silently
    scoring those as agent failures would understate accuracy."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in conn.execute(sql).fetchall()], True
    except sqlite3.Error:
        return [], False
    finally:
        conn.close()


def _norm(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    return value


def results_match(predicted: list[tuple], gold: list[tuple], ordered: bool) -> bool:
    pred = [tuple(_norm(v) for v in row) for row in predicted]
    want = [tuple(_norm(v) for v in row) for row in gold]
    if ordered:
        return pred == want
    return Counter(pred) == Counter(want)


def evaluate_one(q: dict, llm_factory, settings) -> dict[str, Any]:
    path = db_path(q["db_id"])
    executor = SqliteExecutor(path, max_rows=EVAL_MAX_ROWS,
                              timeout_s=settings.query_timeout_s)
    gold_rows, gold_ok = run_gold(q["SQL"], path)
    ordered = "order by" in q["SQL"].lower()

    started = time.perf_counter()
    # BIRD ships an `evidence` hint with each question; it is part of the task,
    # and omitting it would be scoring a harder benchmark than the one everyone
    # else reports on.
    question = q["question"]
    if q.get("evidence"):
        question = f"{question}\n\nHint: {q['evidence']}"
    result = answer(question, llm_factory(q), executor, db_id=q["db_id"],
                    max_repairs=settings.max_repair_attempts, max_rows=EVAL_MAX_ROWS)
    elapsed = (time.perf_counter() - started) * 1000

    truncated = (len(result.rows) >= EVAL_MAX_ROWS or len(gold_rows) >= EVAL_MAX_ROWS)
    scorable = gold_ok and not truncated
    correct = (
        scorable
        and result.status is AnswerStatus.ANSWERED
        and results_match(result.rows, gold_rows, ordered)
    )
    return {
        "question_id": q["question_id"], "db_id": q["db_id"],
        "difficulty": q.get("difficulty"), "question": q["question"],
        "gold_sql": q["SQL"], "predicted_sql": result.sql,
        "status": str(result.status),
        "abstain_reason": str(result.abstain_reason) if result.abstain_reason else None,
        "correct": correct, "gold_executable": gold_ok,
        "scorable": scorable, "truncated": truncated,
        "gold_rows": len(gold_rows), "returned_rows": len(result.rows),
        "attempts": result.attempts, "assumptions": result.assumptions,
        "retrieved_tables": result.retrieved_tables,
        "llm_calls": result.usage.llm_calls, "cached_calls": result.usage.cached_calls,
        "prompt_tokens": result.usage.prompt_tokens,
        "completion_tokens": result.usage.completion_tokens,
        "latency_ms": round(elapsed, 1),
        "error": result.error,
    }


def summarise(records: list[dict]) -> dict[str, Any]:
    n = len(records)
    if n == 0:
        return {"n": 0}
    scored = [r for r in records if r.get("scorable", True)]
    answered = [r for r in scored if r["status"] == "answered"]
    correct = [r for r in scored if r["correct"]]
    by_diff: dict[str, dict[str, int]] = {}
    for r in scored:
        b = by_diff.setdefault(r["difficulty"] or "unknown", {"n": 0, "correct": 0})
        b["n"] += 1
        b["correct"] += int(r["correct"])
    if not scored:
        return {"n": n, "scored": 0}
    return {
        "n": n,
        "scored": len(scored),
        "unscorable": n - len(scored),
        "execution_accuracy": round(len(correct) / len(scored), 4),
        "answered": len(answered),
        "abstained": sum(1 for r in scored if r["status"] == "abstained"),
        "precision_when_answered": (
            round(len(correct) / len(answered), 4) if answered else None
        ),
        "needed_repair": sum(1 for r in scored if r["attempts"] > 1),
        "mean_attempts": round(statistics.mean(r["attempts"] for r in records), 2),
        "median_latency_ms": round(statistics.median(r["latency_ms"] for r in records), 1),
        "total_llm_calls": sum(r["llm_calls"] for r in records),
        "total_prompt_tokens": sum(r["prompt_tokens"] for r in records),
        "gold_unexecutable": sum(1 for r in records if not r["gold_executable"]),
        "by_difficulty": {
            k: {**v, "accuracy": round(v["correct"] / v["n"], 4)}
            for k, v in sorted(by_diff.items())
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=15,
                    help="questions to score (free Gemini tier allows ~20 calls/day)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--oracle", action="store_true",
                    help="feed gold SQL instead of calling a model; validates the harness")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    settings = get_settings()
    seed = args.seed if args.seed is not None else settings.seed
    questions = load_questions()
    sample = stratified_sample(questions, args.limit, seed)

    tag = "oracle" if args.oracle else settings.model
    out = args.out or REPO_ROOT / "evaluation" / "datasets" / f"bird_minidev_{tag}.json"
    previous: dict[int, dict] = {}
    if out.exists():
        prior = json.loads(out.read_text())
        previous = {r["question_id"]: r for r in prior.get("records", [])}
        print(f"resuming: {len(previous)} already scored in {out.name}")

    # Built once: constructing the client per question would re-read config and
    # re-open a session 500 times for no benefit.
    real_llm = None if args.oracle else build_llm(
        settings, cache_dir=REPO_ROOT / ".cache" / "llm")

    def factory(q):
        if args.oracle:
            return StubLLM([SQLDraft(sql=q["SQL"], assumptions=["oracle: gold SQL"])])
        return real_llm

    records: list[dict] = []
    for i, q in enumerate(sample, 1):
        if q["question_id"] in previous:
            records.append(previous[q["question_id"]])
            continue
        if not db_path(q["db_id"]).exists():
            print(f"  [{i}/{len(sample)}] skip {q['db_id']}: database not fetched")
            continue
        try:
            record = evaluate_one(q, factory, settings)
        except Exception as exc:
            print(f"  [{i}/{len(sample)}] ERROR {q['question_id']}: {exc}")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(
                {"summary": summarise(records), "seed": seed, "model": tag,
                 "records": records}, indent=2))
            print(f"\npartial results saved to {out}")
            raise SystemExit(1) from exc
        records.append(record)
        mark = "OK " if record["correct"] else "MISS"
        print(f"  [{i}/{len(sample)}] {mark} {record['difficulty'][:4]:<4} "
              f"{record['db_id'][:22]:<22} attempts={record['attempts']}")

    summary = summarise(records)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"summary": summary, "seed": seed, "model": tag, "records": records}, indent=2))

    print("\n" + "=" * 62)
    print(f"  model                 {tag}")
    print(f"  questions             {summary['n']}  (seed {seed})")
    if summary.get("unscorable"):
        print(f"  unscorable            {summary['unscorable']}  "
              f"(gold failed, or result above the {EVAL_MAX_ROWS:,}-row cap)")
    print(f"  execution accuracy    {summary['execution_accuracy']:.1%}")
    print(f"  answered / abstained  {summary['answered']} / {summary['abstained']}")
    if summary["precision_when_answered"] is not None:
        print(f"  correct when answered {summary['precision_when_answered']:.1%}")
    print(f"  needed repair         {summary['needed_repair']}")
    print(f"  mean attempts         {summary['mean_attempts']}")
    print(f"  median latency        {summary['median_latency_ms']:.0f} ms")
    for name, b in summary["by_difficulty"].items():
        print(f"    {name:<12} {b['correct']}/{b['n']}  {b['accuracy']:.1%}")
    print("=" * 62)
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
