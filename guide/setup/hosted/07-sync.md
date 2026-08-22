# Step 7: Sync config and verify

The four boot vars from Step 6 are enough for the service to start. Everything
else — `LLM_PROVIDER`, the provider credential, model vars, and operational
settings — is pushed in one shot with `--sync-env`.

## Get a Render API key

`--sync-env` and `doctor`/`deploy`'s live checks need a `RENDER_API_KEY` to
act on your behalf — this is separate from anything set on the Render
service itself (Step 6's warning). Get one from the Render dashboard →
**Account Settings → API Keys**, then set it as `RENDER_API_KEY` locally in
`.env`. It's operator-local tooling, never something the service itself
sees.

With `RENDER_API_KEY` set locally, this is a complete, repeatable deploy:

=== "bash"

    ```bash
    PUBLIC_BASE_URL=https://<your-service>.onrender.com uv run python -m scripts.deploy --sync-env
    ```

=== "PowerShell"

    ```powershell
    $env:PUBLIC_BASE_URL = "https://<your-service>.onrender.com"
    uv run python -m scripts.deploy --sync-env
    ```

See [What `--sync-env` pushes](../../reference/sync-env.md) for the exact
push set — it's provider-derived, not a fixed list, so it's documented
there rather than restated here.

## Budget your time

Before triggering anything, `--sync-env` waits for any deploy already in
progress to settle — worst case that's up to 900s waiting for the in-flight
one, plus up to 900s for the one it triggers itself, so **budget up to ~30
minutes** in the rare worst case. In practice, a warm redeploy with nothing
already in flight has taken well under a minute.

## Claude Code shortcut

Claude Code users can run `/deploy` instead, which wraps the same CLI.

## Next

Continue to [Step 8: add the keep-warm pinger](08-pinger.md).
