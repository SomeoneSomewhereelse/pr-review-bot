"""Compare app/providers/pricing.py's groq rates against Groq's live catalog.

    uv run python -m scripts.pricing_check

Groq's /openai/v1/models returns pricing.prompt / pricing.completion (USD per
token) inline -- which is where the existing groq entry came from. This is a
METADATA call, not a generation call: CLAUDE.md's one-deliberate-live-call
rule governs completions, not catalog listings.

Google publishes no equivalent endpoint, so gemini/vertex entries stay manual;
their Rate.source_url is where to re-check them by hand.

Prints paste-ready _RATES lines for anything missing or drifted, and never
prints or transmits a credential value -- the key is read from Settings and
attached as a header, never logged.
"""
from __future__ import annotations

import argparse
import sys
from typing import NamedTuple

import httpx

from app.config import settings
from app.providers import pricing

_GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
_HTTP_TIMEOUT = 10.0


class Comparison(NamedTuple):
    """Two findings of very different weight, kept apart on purpose.

    ``drift`` is a real problem: a rate this project USES has changed, so
    every cost estimate it has produced since is wrong. ``missing`` is not a
    problem at all -- Groq's catalog carries dozens of models this project
    will never run (whisper, guard, and so on), and an unpriced model is
    supported anyway (design spec section 6a). Folding the two together made
    a routine run print a wall of paste-ready lines and exit non-zero every
    single time, which trains an operator to ignore the tool.
    """

    drift: list[str]
    missing: list[str]


def compare(catalog: dict[str, tuple[float, float]]) -> Comparison:
    """Compare `catalog` (model -> USD per 1M tokens) against the rate table."""
    drift: list[str] = []
    missing: list[str] = []
    for model, (rate_in, rate_out) in sorted(catalog.items()):
        known = pricing.rate_for("groq", model)
        if known is None:
            missing.append(
                f'("groq", "{model}"): '
                f'Rate({rate_in}, {rate_out}, _GROQ_PRICING, "<today>"),'
            )
        elif (known.rate_in, known.rate_out) != (rate_in, rate_out):
            drift.append(
                f"groq/{model} table says ({known.rate_in}, {known.rate_out}), "
                f"catalog says ({rate_in}, {rate_out}) "
                f"[verified {known.verified}, source {known.source_url}]"
            )
    return Comparison(drift, missing)


def _fetch_groq_catalog() -> dict[str, tuple[float, float]]:
    if not settings.groq_api_key:
        raise SystemExit("GROQ_API_KEY is not set; nothing to check")
    response = httpx.get(
        _GROQ_MODELS_URL,
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        timeout=_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    catalog: dict[str, tuple[float, float]] = {}
    for entry in response.json().get("data", []):
        price = entry.get("pricing") or {}
        prompt, completion = price.get("prompt"), price.get("completion")
        if prompt is None or completion is None:
            continue
        # the endpoint reports USD per token; the table stores USD per 1M.
        # Round the conversion so exact-float-equality in compare() doesn't
        # report false drift on values that should match (e.g. 7.9e-7 * 1e6
        # == 0.7899999999999999 in raw float arithmetic, not 0.79).
        catalog[entry["id"]] = (
            round(float(prompt) * 1e6, 6),
            round(float(completion) * 1e6, 6),
        )
    return catalog


def main(argv: list[str] | None = None) -> int:
    """Exit 1 ONLY on drift. A model the table does not carry is normal and
    exits 0 -- see Comparison's docstring for why the two are not equivalent."""
    parser = argparse.ArgumentParser(
        description="Compare app/providers/pricing.py's groq rates against Groq's catalog"
    )
    parser.add_argument(
        "--show-missing",
        action="store_true",
        help="list paste-ready _RATES lines for catalog models the table lacks "
        "(off by default: Groq's catalog carries dozens this project never runs)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    result = compare(_fetch_groq_catalog())

    if result.missing:
        print(f"pricing: {len(result.missing)} catalog model(s) not in the rate table", end="")
        print(" --" if not args.show_missing else ":")
        if args.show_missing:
            for line in result.missing:
                print(f"  {line}")
        else:
            print(" re-run with --show-missing to list them")

    if not result.drift:
        print("pricing: every groq rate this project uses matches the live catalog")
        return 0
    print("pricing DRIFT detected -- estimates using these rates are wrong:")
    for line in result.drift:
        print(f"  {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
