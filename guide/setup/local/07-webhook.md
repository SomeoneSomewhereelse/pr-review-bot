# Step 7: Register the webhook

```bash
uv run python -m scripts.deploy
```

This points the GitHub App's webhook at your current `PUBLIC_BASE_URL` (only
patching it if it's wrong — see the chicken-and-egg note below) and runs the
same verification checklist `scripts/doctor.py` composes from.

On this track, expect the **Render** and **pinger** rows to report `SKIP`
cleanly with no `RENDER_API_KEY` set — that is expected here, not a problem.
Those checks exist for the hosted track; nothing in `app/` knows Render
exists at all.

## Re-run this each session

Because a quick tunnel's URL is ephemeral (previous step), the webhook you
registered last session is pointing at a URL that no longer exists.
`scripts/deploy.py` corrects the App's webhook URL as a normal part of
verification — it is designed to tolerate this, not a workaround — so simply
re-running it each time you restart the tunnel is enough.

## A credential-free "is it up?" check

To check just that the service is reachable, without needing any App
credential:

```bash
uv run python -m scripts.deploy --health-only
```

This is the portable way to check `/healthz` — a literal `curl` command
behaves differently across platforms (on Windows PowerShell it's aliased to
`Invoke-WebRequest`, which takes different arguments), so this project's own
health check is used instead of any shell HTTP client.

## Next

Continue to [Step 8: run the service](08-run.md).
