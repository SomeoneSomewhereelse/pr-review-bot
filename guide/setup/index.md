# Setup

Getting from a fresh clone to a first posted review comment is eight steps.
The first four are the same no matter how you plan to run the service; the
last four depend on whether you run it on your own machine or deploy it.

## Steps 1–4: shared

These are covered by the next four pages, in order:

1. **Install prerequisites** — Python, `uv`, git, and a way to reach Postgres.
2. **Create the GitHub App** — the identity the bot uses to read PRs and post
   comments.
3. **Install the App on your repo(s)** — a browser-only step; GitHub does not
   let an App install itself.
4. **Configure an LLM provider** — pick a provider, get a key, set it.

Run `uv run python -m bot.scripts.doctor` at any point — it tells you which of
these (and the four that follow) are still outstanding.

## Steps 5–8: choose a track

Once the shared steps are done, the remaining steps diverge by where the
service runs:

| | Local | Hosted |
|---|---|---|
| Public URL | needs a tunnel (`cloudflared`) | stable, from Render |
| Cost | nothing to pay for | free tiers throughout |
| URL stability | changes every restart | stable across restarts |
| Remaining steps | mostly your own terminal | four browser/dashboard steps |

- **Local** — run the engine on your own machine against a tunnel, for
  development and debugging. Continue to
  [local/05-postgres.md](local/05-postgres.md).
- **Hosted** — deploy to Render with Supabase for the queue, for a durable,
  always-on reviewer. Continue to
  [hosted/05-supabase.md](hosted/05-supabase.md).

Both tracks share steps 1–4 above, so a wrong guess this early costs
nothing — `scripts/doctor.py` re-detects which track you're on from your
environment (a `RENDER_API_KEY` or an `onrender.com` URL means hosted;
anything else means local).
