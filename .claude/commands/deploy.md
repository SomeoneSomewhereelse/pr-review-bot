---
description: Verify the hosted Render + Supabase deployment and register the webhook
---

Run the deploy verification CLI with `--json` so you can parse the result
programmatically:

```bash
uv run python -m bot.scripts.deploy --json
```

This prints one JSON object: `checks` (a list of `{name, status, detail}`,
one per row, in the order documented in `guide/reference/checks.md`),
`summary` (counts of passed/warned/failed/skipped), `guide_url`, and `table` —
the exact plain-text table the CLI prints without `--json`. Show `table` to
the user verbatim — it is already terse by design, so do not summarize,
reformat, or add emoji to it — and use `checks`/`summary` yourself to decide
what to do next without re-parsing the table's indentation-based continuation
lines.

If the exit code is non-zero, help the user act on each check whose `status`
is `FAIL`, using its `detail` (already a hint, not just an observation). Full
explanations of each check live in `guide/operations/deploy.md`; read that
page (and `guide/reference/checks.md` for the per-check explanations) before
speculating about a cause.

If the diagnosis is that the Render service's environment variables have
drifted from the local `.env`, the follow-up is:

```bash
uv run python -m bot.scripts.deploy --sync-env --json
```

That pushes the changed variables, triggers a deploy, waits for it to go live,
and then re-runs the checklist (also emitted as the same JSON shape, on
success). It requires `RENDER_API_KEY` and refuses to run if any local value
is empty. Note that `--sync-env`'s own progress lines (which variables were
pushed, deploy status while waiting) are plain text on stdout/stderr, not
JSON, and print before the final `--json` checklist object — read those
directly rather than expecting them inside the JSON payload. If `--sync-env`
refuses outright (e.g. a missing key, an active DB override), it exits
non-zero before the checklist ever runs, so there is no JSON object at all for
that failure — the plain-text refusal message on stderr is the only output to
read in that case.

This command holds no verification logic of its own — `bot/scripts/deploy.py` is the
tool, and it works identically (including `--json`) for people who do not use
Claude Code.
