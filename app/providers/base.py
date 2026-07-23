"""Provider protocol + the uniform response shape every adapter returns.

Design decision (load-bearing for later steps — specialists.py, orchestrator.py):
``complete()`` does NOT return a bare validated Pydantic model. It returns
``LLMResponse``, which carries the model's raw text, real token usage, and a
best-effort ``parsed`` field.

Why: ``validate.py`` needs to know whether the raw output failed schema
validation so it can issue a single repair retry — and it needs BOTH calls'
token usage even when the first attempt fails (a wasted call still costs
tokens). If ``complete()`` only ever returned ``BaseModel | None`` there
would be nowhere to carry usage on a failed attempt. So each provider itself
attempts to parse+validate ``raw_text`` against ``schema`` (never raising on
a *validation* failure — only on a genuine transport/API error) and reports
the outcome via ``.parsed`` (``None`` on failure).

Later steps depend on this shape:
- ``validate.py`` composes one or two ``LLMResponse``s into a single
  ``ValidatedResult`` (ok / parsed / tokens_in / tokens_out / error).
- ``specialists/base.py`` (a later step) will read tokens_in/tokens_out off
  whatever ``validate.py`` returns to populate ``SpecialistResult``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


@dataclass
class LLMResponse:
    """Uniform provider return value.

    ``parsed`` is ``None`` when ``raw_text`` failed to parse as JSON or
    failed schema validation — providers never raise for that case, only
    for genuine transport/API errors.
    """

    raw_text: str
    tokens_in: int
    tokens_out: int
    parsed: BaseModel | None = None


class LLMProvider(Protocol):
    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse: ...
