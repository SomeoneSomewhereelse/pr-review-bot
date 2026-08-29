# Step 8: Run the service

In your **first** terminal (the tunnel is running in the second):

```bash
uv run uvicorn bot.main:app --host 0.0.0.0 --port 8000
```

This process itself is what "keeps the service warm" locally — there is no
Render free-tier sleep to work around, so nothing extra is needed here
(unlike the hosted track's UptimeRobot pinger).

The dashboard (`GET /`) requires signing in at `/login` first with the
`DASHBOARD_*` credential you set in `.env` — no need for HTTPS to test this
locally: every major browser exempts loopback addresses from the `Secure`
cookie flag, so a `Secure` session cookie works fine over plain
`http://localhost`. If login seems to succeed but keeps bouncing back to
`/login`, that's a sign the cookie isn't being set/sent at all (e.g. a
mismatched host/port between the browser and the running server), not an
HTTPS requirement.

## Open a real PR to review

This uses the repo you picked, installed the App on, and set as
`GITHUB_TARGET_REPO` back in [Step 3](../03-install-app.md). If you skipped
that, do it now — `uv run python -m bot.scripts.doctor`'s `gh-auth` and
`target-repo` rows will FAIL with the specific account/repo mismatch if
there is one, rather than you finding out from `seed_demo_pr` failing below.

```bash
uv run python -m bot.scripts.seed_demo_pr
```

This clones the configured test repo, plants known-bad code from
`bot/fixtures/bad_code/`, and opens a real PR against it — which GitHub then
delivers to your tunnel as a webhook event.

## What a good result looks like

Within roughly 15 seconds of the PR opening, a single comment appears on it
naming real findings across all three sections (security, performance, code
quality — a section with no findings still renders, just as
"✅ no findings"), with a footer along the lines of:

```
Runtime 11.4s · 4,910 tok in / 780 tok out · est. $0.0021 · provider: groq
```

Runtime and token counts always appear; the cost estimate only appears if
the active model has a priced entry in this project's pricing table — an
unpriced model still runs and reviews normally, just without the `est.`
fragment.

`bot/fixtures/bad_code/` plants three specific problems, so you can check that
the review caught the right things rather than only that a comment appeared:

| Planted issue | Where | Which specialist should catch it |
| --- | --- | --- |
| a hardcoded credential | a module-level API key constant | security |
| an N+1 query | one HTTP call per account, inside the loop over accounts | performance |
| a magic number | a bare threshold in the high-usage filter | code quality |

A review that names all three is working as designed. One that names two is
still a real review — the specialists are LLM calls, not a fixed rule set —
but three is what the fixture is built to produce.

That comment is the whole point of this project: a fresh PR from a fresh
clone, reviewed automatically, no manual step in between.
