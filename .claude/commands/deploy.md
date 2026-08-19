---
description: Verify the hosted Render + Supabase deployment and register the webhook
---

Run the deploy verification CLI and report its results:

```bash
uv run python -m scripts.deploy
```

Show the printed table to the user verbatim — it is already terse by design, so
do not summarize, reformat, or add emoji to it.

If the exit code is non-zero, help the user act on each line marked `FAIL`,
using the hint that line already printed. Full explanations of each check live
in `guide/operations/deploy.md`; read that page (and
`guide/reference/checks.md` for the per-check explanations) before
speculating about a cause.

If the diagnosis is that the Render service's environment variables have
drifted from the local `.env`, the follow-up is:

```bash
uv run python -m scripts.deploy --sync-env
```

That pushes the changed variables, triggers a deploy, waits for it to go live,
and then re-runs the checklist. It requires `RENDER_API_KEY` and refuses to run
if any local value is empty.

This command holds no verification logic of its own — `scripts/deploy.py` is the
tool, and it works identically for people who do not use Claude Code.
