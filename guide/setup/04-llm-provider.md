# Step 4: Configure an LLM provider

Each specialist call goes through one of three providers: `gemini`, `groq`,
or `vertex`. `LLM_PROVIDER` has **no default** — the service refuses to
start without it set to one of those three values.

## Pick a provider

**Groq is the recommended starting point**: it has a free tier, needs no
card, and is what every live rehearsal of this project has used.

- **Groq** — <https://console.groq.com/keys>. Free tier, no card.
- **Gemini** (AI Studio) — a free API key, but Google's free-tier keys have
  been known to trip an account-level Trust & Safety flag under heavy
  testing; see the guide's *Provider history* page if you hit a persistent
  `403`.
- **Vertex AI** — a GCP service-account identity rather than an API-key
  string; requires GCP billing to be enabled on the project.

## Set it

Run this project's own prompt-driven script yourself — it scaffolds `.env`
and `.env.config` from the committed templates if they don't exist yet
(e.g. if you used Step 2's manual fallback and only have `.env` so far), and
interactively prompts for every setting either file declares, `LLM_PROVIDER`
included — so answer `groq` (or your chosen provider) when it asks:

```bash
uv run python -m scripts.init_env
```

When it asks for the matching credential, that's the secret half — it
writes straight to `.env` and is never echoed back, so no credential value
ever needs to pass through an agent or a command-line argument.

`LLM_PROVIDER` itself is operational config, not a secret, so once
`.env.config` exists you can also hand-edit the line there directly
(`LLM_PROVIDER=groq`) any time you want to switch providers later, without
re-running `init_env`.

## Model pricing is optional

A model with no entry in this project's pricing table still runs — the
posted PR comment simply appears without a cost estimate, and
`scripts/deploy.py`'s `pricing` check reports it as a warning, not a
blocker. See [Model pricing](../reference/pricing.md) for the models this
project has verified rates for.

## Next

Steps 1–4 are done. Continue to
[local/05-postgres.md](local/05-postgres.md) (Local track) or
[hosted/05-supabase.md](hosted/05-supabase.md) (Hosted track) — see the
[setup overview](index.md) if you haven't chosen one yet.
