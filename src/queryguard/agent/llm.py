"""The only part of the system that talks to a model.

Everything else in `agent/` is deterministic, which is deliberate: it means the
graph can be tested end-to-end against `StubLLM` with no key, no network and no
cost, and the tests still exercise the real nodes, the real validator and the
real database.

`CachedLLM` exists because the eval harness re-runs the same questions across
system versions. Without it, changing a prompt and re-scoring means paying for
every unchanged question again. The key includes the model and temperature, so
switching either one correctly misses the cache rather than silently serving a
stale answer.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol

from queryguard.models import SQLDraft, Usage


class DraftLLM(Protocol):
    def draft(self, system: str, user: str) -> tuple[SQLDraft, Usage]: ...


class StubLLM:
    """Replays scripted drafts. Used by the tests and by --dry-run.

    Takes a list so a test can script an initial bad draft followed by a good
    repair, which is the only way to exercise the repair edge deterministically.
    """

    def __init__(self, drafts: list[SQLDraft]):
        self._drafts = list(drafts)
        self.calls: list[tuple[str, str]] = []

    def draft(self, system: str, user: str) -> tuple[SQLDraft, Usage]:
        self.calls.append((system, user))
        if not self._drafts:
            raise AssertionError("StubLLM ran out of scripted drafts")
        return self._drafts.pop(0), Usage(llm_calls=1)


class GeminiDraftLLM:
    def __init__(self, api_key: str, model: str, temperature: float = 0.0):
        from langchain_google_genai import ChatGoogleGenerativeAI

        self._llm = ChatGoogleGenerativeAI(
            model=model, temperature=temperature, google_api_key=api_key,
        ).with_structured_output(SQLDraft, include_raw=True)

    def draft(self, system: str, user: str) -> tuple[SQLDraft, Usage]:
        out = self._llm.invoke([("system", system), ("human", user)])
        parsed = out.get("parsed") if isinstance(out, dict) else out
        if parsed is None:
            # Structured decoding failed. Surfacing it as an unanswerable draft
            # keeps the graph on one path instead of raising through the loop.
            err = out.get("parsing_error") if isinstance(out, dict) else None
            return (
                SQLDraft(sql="", answerable=False,
                         assumptions=[f"model returned unparseable output: {err}"]),
                Usage(llm_calls=1),
            )
        usage = Usage(llm_calls=1)
        raw = out.get("raw") if isinstance(out, dict) else None
        meta = getattr(raw, "usage_metadata", None) or {}
        if meta:
            usage.prompt_tokens = meta.get("input_tokens", 0) or 0
            usage.completion_tokens = meta.get("output_tokens", 0) or 0
        return parsed, usage


class CachedLLM:
    """Disk cache keyed by (model, temperature, system, user)."""

    def __init__(self, inner: DraftLLM, cache_dir: Path, tag: str):
        self._inner = inner
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._tag = tag

    def _key(self, system: str, user: str) -> Path:
        h = hashlib.sha256(f"{self._tag}\x00{system}\x00{user}".encode()).hexdigest()
        return self._dir / f"{h}.json"

    def draft(self, system: str, user: str) -> tuple[SQLDraft, Usage]:
        path = self._key(system, user)
        if path.exists():
            payload = json.loads(path.read_text())
            return SQLDraft.model_validate(payload), Usage(llm_calls=0, cached_calls=1)
        draft, usage = self._inner.draft(system, user)
        path.write_text(draft.model_dump_json())
        return draft, usage


def build_llm(settings, cache_dir: Path | None = None) -> DraftLLM:
    """Wire the real model from settings. Raises if no key is configured."""
    if settings.google_api_key is None:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set. Copy .env.example to .env and add a key "
            "from https://aistudio.google.com/apikey"
        )
    llm: DraftLLM = GeminiDraftLLM(
        api_key=settings.google_api_key.get_secret_value(),
        model=settings.model,
        temperature=settings.temperature,
    )
    if settings.llm_cache:
        tag = f"{settings.model}|{settings.temperature}"
        llm = CachedLLM(llm, cache_dir or Path(".cache/llm"), tag)
    return llm
