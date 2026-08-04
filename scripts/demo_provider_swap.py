"""Step 7 live-swap demo: prove LLM_PROVIDER genuinely swaps behavior.

Runs the real orchestrator against a real PR twice, swapping LLM_PROVIDER at
runtime via monkeypatching app.config.settings (no server restart needed —
proves the abstraction is a true runtime seam, not just an env-file toggle):

  1. LLM_PROVIDER=groq   -> real success (as proven in steps 5-6).
  2. LLM_PROVIDER=gemini -> real transport failure (403, account-flagged
     per SETUP.md/CLAUDE.md) -> every specialist's own never-raise contract
     (specialists/base.py) catches it and returns status="failed" -> the
     orchestrator's asyncio.gather merge (step 6) still produces a full
     ReviewResult and posts a coherent comment showing 3 failed rows. No
     crash, nothing silently dropped.

This is a genuine, non-simulated demonstration of both provider-swappability
and the "partial/total failure is always visible" resilience guarantee
(CLAUDE.md's convention) — arguably a *better* proof than an all-success path,
since it's exercising the real catch-and-report code, not just the happy path.

Ends by re-running with groq so the demo PR's final comment state reflects a
successful review, not a failed one.
"""

from __future__ import annotations

import asyncio
import sys

from app.config import settings
from app.orchestrator import run_review

DEFAULT_REPO = settings.github_target_repo
DEFAULT_PR_NUMBER = 2


async def _run_with_provider(provider: str, repo_full_name: str, pr_number: int):
    settings.llm_provider = provider
    print(f"\n--- LLM_PROVIDER={provider} ---")
    result = await run_review(repo_full_name, pr_number)
    print(f"provider={result.provider} model={result.model} elapsed_ms={result.total_elapsed_ms}")
    for r in result.results:
        if r.status == "ok":
            print(f"  {r.name}: ok, {len(r.findings)} finding(s)")
        else:
            print(f"  {r.name}: FAILED — {r.error}")
    return result


async def main() -> None:
    repo_full_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REPO
    pr_number = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PR_NUMBER

    original_provider = settings.llm_provider
    try:
        groq_result = await _run_with_provider("groq", repo_full_name, pr_number)
        assert all(r.status == "ok" for r in groq_result.results), "expected groq to fully succeed"

        gemini_result = await _run_with_provider("gemini", repo_full_name, pr_number)
        assert all(r.status == "failed" for r in gemini_result.results), (
            "expected every specialist to fail gracefully under the real Gemini block"
        )

        print("\nRestoring a successful comment (re-running with groq) ...")
        await _run_with_provider("groq", repo_full_name, pr_number)

        print(
            "\nSUCCESS: LLM_PROVIDER is a genuine runtime seam — groq succeeds, "
            "gemini fails, and BOTH produce a coherent posted comment with no crash."
        )
    finally:
        settings.llm_provider = original_provider


if __name__ == "__main__":
    asyncio.run(main())
