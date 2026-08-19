# Step 8: Run the service

In your **first** terminal (the tunnel is running in the second):

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

This process itself is what "keeps the service warm" locally — there is no
Render free-tier sleep to work around, so nothing extra is needed here
(unlike the hosted track's UptimeRobot pinger).

## Open a real PR to review

```bash
uv run python -m scripts.seed_demo_pr
```

This clones the configured test repo, plants known-bad code from
`fixtures/bad_code/`, and opens a real PR against it — which GitHub then
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

That comment is the whole point of this project: a fresh PR from a fresh
clone, reviewed automatically, no manual step in between.
