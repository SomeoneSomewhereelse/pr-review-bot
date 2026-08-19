# Tuning cooldowns and usage caps

The re-review cooldown and the per-key usage cap are both **database-only
settings**: `.env.config` is the source of truth for their values, but they
are **never a Render env var at all**, unlike the rest of `.env.config`.
They live only in the `runtime_config` table (the same table
[provider/key overrides](overrides.md) write to), because the dispatcher
needs to be able to change them with no redeploy.

```bash
uv run python -m scripts.deploy --sync-config-db   # push .env.config's values into runtime_config
```

Needs only `DATABASE_URL` — no `RENDER_API_KEY`, no checklist, no redeploy.
`--sync-env` also runs this same push as one of its own steps, so a normal
full deploy keeps the database in sync too; `--sync-config-db` is only the
fast, redeploy-free path for changing just this. Either way, the change
takes effect on the **next ticket the dispatcher claims**.

## The re-review cooldown

```bash
# .env.config
DISPATCHER_REREVIEW_COOLDOWN_SECONDS=30
DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS=3600
DISPATCHER_REREVIEW_COOLDOWN_FACTOR=1.5
```

These three settings escalate the wait between re-reviews of the same PR:
`_SECONDS` is the base, `_FACTOR` multiplies it on each successive push, and
`_MAX_SECONDS` caps how high it can climb. `--sync-config-db` refuses the
write (`exit 2`, nothing written) if the resolved base/cap/factor would be
invalid (`factor < 1.0`, `base > cap`, or a non-positive base/cap) — that
combination would otherwise write successfully but be silently discarded on
every read. To go back to the built-in defaults (300s/3600s/2.0), remove the
lines from `.env.config` and re-run `--sync-config-db`.

## The per-key usage cap

```bash
# .env.config
KEY_USAGE_TOKEN_CAP=20000        # tokens/day for the ACTIVE key slot
KEY_USAGE_RESET_TIME_UTC=04:00   # when the day rolls over (default 04:00 UTC)
```

`KEY_USAGE_TOKEN_CAP` is **unset by default** — set it and nothing changes
until you do. When it's set, the dispatcher checks the currently-active
`(provider, key slot)`'s usage so far today *before* starting a review; at
or over the cap it defers the ticket to the next reset rather than making
the call, and the PR gets a notice saying so, worded distinctly from a
provider rate limit. This is the proactive counterpart to the reactive 429
backoff: it's what stops a bug or a PR spike from burning a free-tier
credit before anyone notices. The reset time takes any `HH:MM` (or
`HH:MM:SS`), not whole hours — set it a couple of minutes out to watch a
cap reset during a demo instead of waiting for the next hour boundary.

Three things worth knowing:

- **The cap is per key slot, not global.** Swapping slots with
  `uv run python -m scripts.set_override groq --index 1` immediately grants
  a fresh budget, exactly as key rotation already works — nothing
  auto-swaps on a breach; a human decides. That fresh budget applies to the
  next ticket claimed; a ticket already deferred by the cap still waits for
  its scheduled reset (raising or clearing the cap doesn't retroactively
  release it).
- **Usage survives restarts.** It's summed from the persisted `reviews`
  history, not counted in memory, so a redeploy neither resets nor loses it.
- **A usage-check failure fails open.** A broken usage query logs and lets
  the review proceed rather than blocking every review on it.

The cap is a ceiling on when the *next* review may start, not on the exact
daily total: a review's real token usage is only known once it finishes, so
the run that crosses the line is allowed to complete.

## Next

- [Switching providers and API keys](overrides.md)
- [Deploying and verifying](deploy.md)
