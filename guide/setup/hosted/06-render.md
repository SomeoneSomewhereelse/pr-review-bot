# Step 6: Create the Render service

## Create it

1. Go to <https://render.com/dashboard>.
2. Click **New +** → **Blueprint** → connect your GitHub repo and point it
   at `render.yaml` at the repo root.

`render.yaml` declares `runtime: docker` with a `dockerfilePath`, so Render
builds and runs this project's `Dockerfile` as-is — there is no separate
Build/Start command to configure; the container's entrypoint is the
Dockerfile's own `CMD`
(`uv run --no-dev uvicorn app.main:app --host 0.0.0.0 --port 8000`).

!!! note "Docs-only pushes never trigger a deploy"
    `render.yaml` sets `buildFilter.ignoredPaths: ["**/*.md"]`, so a push
    that only touches a Markdown file never triggers a build/deploy — the
    Dockerfile never copies any of it into the image.

## Set exactly four env vars

In the **Environment** tab, set these four — and only these four — to get
the service booting. They are what `scripts/deploy.py` checks for on the
service (its `_BOOT_CREDENTIAL_NAMES`):

- `GITHUB_APP_ID`
- `GITHUB_APP_PRIVATE_KEY`
- `GITHUB_WEBHOOK_SECRET`
- `DATABASE_URL` — the Supabase Session-mode pooler string from Step 5

Everything else — `LLM_PROVIDER`, the provider credential, model vars, and
the rest — is what `--sync-env` pushes for you in Step 7. There's no need to
hand-enter them here.

!!! warning "RENDER_API_KEY is not a service env var"
    `RENDER_API_KEY` is operator-local tooling — it lives only in your own
    `.env`, where `scripts/deploy.py` and `scripts/doctor.py` read it to set
    env vars and read logs on your behalf. Never add it to `render.yaml` and
    never give it to the Render service itself.

## Deploy and verify

Click **Deploy**. Before considering this step done, verify:

- The deploy's logs end with uvicorn's `Application startup complete.`
- `https://<your-service>.onrender.com/healthz` returns `{"status":"ok"}`.

## Troubleshooting the first deploy

If it fails with `error connecting in 'pool-1'` or a `RuntimeError` about the
connection not opening, the usual cause is a Supabase project that was not
ready yet, or a mistyped pooler string (Step 5). Render does **not** retry
failed deploys automatically, and a first deploy leaves no previous instance
running — fix the value and click **Manual Deploy**.

## Next

Continue to [Step 7: sync config and verify](07-sync.md).
