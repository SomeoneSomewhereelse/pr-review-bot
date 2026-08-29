# Step 7: Register the webhook

```bash
uv run python -m bot.scripts.deploy
```

This points the GitHub App's webhook at your current `PUBLIC_BASE_URL` (only
patching it if it's wrong) and runs the same verification checklist
`bot/scripts/doctor.py` composes from.

## Two rows are expected to look wrong here, not broken

**The webhook itself.** Step 2 had you type in a fake placeholder
(`https://example.invalid/webhook`) because no public URL existed yet — the
App needed *something* there to be created. This command only patches the
webhook when it doesn't already match `PUBLIC_BASE_URL`, so the first run
reports it corrected the URL from that placeholder — that's this
chicken-and-egg resolving itself, not a mistake to go back and fix at Step 2.

**The `health` row.** Expect it to `FAIL` here. It works by answering
`/healthz` through your tunnel, but the local service that would answer it
isn't started until [Step 8](08-run.md) — so right now there's nothing on
the other end of the tunnel to respond at all. Ignore that one row for now;
it turns green once Step 8's `uvicorn` is running, and the `--health-only`
command below re-checks just that row afterward, without needing any App
credential.

On this track, also expect five rows -- `boot-creds-live`, `provider-live`,
`api-key-live`, `render-service`, and `uptime-pinger` -- to report `SKIPPED`
cleanly with no `RENDER_API_KEY` or `UPTIMEROBOT_API_KEY` set — that is
expected here too, not a problem. Those checks exist for the hosted track;
nothing in `bot/` knows Render exists at all.

## Re-run this each session

Because a quick tunnel's URL is ephemeral (previous step), the webhook you
registered last session is pointing at a URL that no longer exists.
`bot/scripts/deploy.py` corrects the App's webhook URL as a normal part of
verification — it is designed to tolerate this, not a workaround — so simply
re-running it each time you restart the tunnel is enough.

## A credential-free "is it up?" check

To check just that the service is reachable, without needing any App
credential:

```bash
uv run python -m bot.scripts.deploy --health-only
```

This is the portable way to check `/healthz` — a literal `curl` command
behaves differently across platforms (on Windows PowerShell it's aliased to
`Invoke-WebRequest`, which takes different arguments), so this project's own
health check is used instead of any shell HTTP client.

## Next

Continue to [Step 8: run the service](08-run.md).
