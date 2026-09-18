"""Tests for the scoring logic.

The comparison function decides every number this project reports, so a bug
here silently rewrites the headline result. These cases are the ones that would
do it quietly rather than loudly.
"""

from __future__ import annotations

from evaluation.harness import results_match, stratified_sample

QUESTIONS = (
    [{"question_id": i, "difficulty": "simple"} for i in range(30)]
    + [{"question_id": 100 + i, "difficulty": "moderate"} for i in range(50)]
    + [{"question_id": 200 + i, "difficulty": "challenging"} for i in range(20)]
)


def test_row_order_ignored_without_order_by():
    assert results_match([(1,), (2,)], [(2,), (1,)], ordered=False)


def test_row_order_enforced_with_order_by():
    assert not results_match([(1,), (2,)], [(2,), (1,)], ordered=True)


def test_duplicate_rows_are_not_collapsed():
    """A set comparison would call these equal. They are not: COUNT(*) differs."""
    assert not results_match([(1,), (1,), (2,)], [(1,), (2,)], ordered=False)


def test_float_noise_tolerated():
    assert results_match([(1.0000000001,)], [(1.0,)], ordered=False)


def test_genuinely_different_numbers_still_fail():
    assert not results_match([(1.01,)], [(1.0,)], ordered=False)


def test_empty_vs_nonempty():
    assert not results_match([], [(1,)], ordered=False)


def test_sample_is_deterministic_for_a_seed():
    a = stratified_sample(QUESTIONS, 15, seed=20260918)
    b = stratified_sample(QUESTIONS, 15, seed=20260918)
    assert [q["question_id"] for q in a] == [q["question_id"] for q in b]


def test_sample_changes_with_seed():
    a = stratified_sample(QUESTIONS, 15, seed=1)
    b = stratified_sample(QUESTIONS, 15, seed=2)
    assert [q["question_id"] for q in a] != [q["question_id"] for q in b]


def test_sample_covers_every_difficulty():
    """A flat sample of 15 can miss `challenging` entirely and flatter the score."""
    sample = stratified_sample(QUESTIONS, 15, seed=20260918)
    assert {q["difficulty"] for q in sample} == {"simple", "moderate", "challenging"}


def test_sample_respects_the_limit():
    assert len(stratified_sample(QUESTIONS, 15, seed=7)) == 15
    assert len(stratified_sample(QUESTIONS, 10_000, seed=7)) == len(QUESTIONS)
