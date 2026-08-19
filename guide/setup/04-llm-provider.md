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

`LLM_PROVIDER` is operational config, not a secret — set it directly in
`.env.config`:

```bash
LLM_PROVIDER=groq
```

The credential itself **is** a secret and belongs in `.env`. Set it by
running this project's own prompt-driven script yourself — it asks for the
real key interactively and writes it for you, so no credential value ever
needs to pass through an agent or a command-line argument:

```bash
uv run python -m scripts.init_env
```

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
