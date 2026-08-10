# Handoff — the deploy CLI can report green while the live service is missing a credential

**Date:** 2026-08-10
**Status:** Open — needs its own planning session (brainstorm + plan), not a quick patch
**Relates to:** `docs/superpowers/specs/2026-08-08-provider-agnostic-config-and-deploy-hardening-design.md`
§3.5 (`provider` check design — the limitation below is already named there,
not newly discovered, but this session hit it for real and it's worth
re-examining now that it's cost real time rather than being theoretical),
`docs/2026-08-10-demo-rehearsal-checkpoint.md` (the session this surfaced
during).

## Context

This surfaced while rehearsing the Zoom demo plan. Wanting to test Gemini as
an alternative provider for a segment, the DB-backed provider override
(`scripts/set_provider.py gemini`) was set, and a PR was opened to trigger a
live review. Every specialist failed with `"No API key was provided."` —
`GEMINI_API_KEY` had never actually been pushed to the Render service
(`render.yaml` declares every provider credential `sync: false`, and no
prior deploy had ever used Gemini live, so nobody had had a reason to push
it before).

**The part worth a fresh look:** `uv run python -m scripts.deploy` was run
*before* this failure was discovered, specifically to check things were in
order, and it reported everything green:

- `config` — **PASS**. By design (§2.1 of the linked spec), this check only
  validates the **environment-configured** provider (`LLM_PROVIDER=groq`
  locally and on Render), deliberately ignoring any active DB override. This
  is stated as intentional, not a gap: a missing environment credential is a
  real latent misconfiguration regardless of what override is active right
  now.
- `provider` — **PASS**. This check exists specifically to catch the
  "override names a provider whose credential is missing" case. It did
  resolve the override correctly (`gemini`) and did check for
  `GEMINI_API_KEY` — but only in the **local** `.env`, which had it (that's
  exactly why the local key had just been verified working via
  `scripts/manual_verify_step4.py`). It has no way to know the credential
  was never pushed to the service actually running the review.

So both checks did exactly what their own documented contracts say they do.
The gap is that **nothing in the CLI actually asks Render what the live
service's environment contains** — every check is either local-`.env`-based
or, for `render-service`, about deploy/build status rather than environment
contents. `README.md`'s "Switching providers without a redeploy" section
already warns about this in prose ("a provider whose key was never pushed
to Render will report `PASS` here and then fail every real review... run
`--sync-env`... which is what actually gets it there") — but a warning in
prose didn't stop this session from hitting it live and losing time
figuring out why reviews were failing despite a clean report.

## What's technically available to fix this

Checked during the rehearsal session: `GET
https://api.render.com/v1/services/{id}/env-vars` (using the same
`RENDER_API_KEY` already used by the `render-service` check and
`--sync-env`) returns the service's **actual live environment variables,
including values** — this is a real, already-authenticated capability, not
something that needs a new credential or permission. Confirmed live, this
session, with your own `RENDER_API_KEY`.

**Important operational note for whoever picks this up:** that same live
check accidentally printed full secret values (not just key names) into a
terminal/conversation transcript during this session — a real "no secret is
ever logged" (`CLAUDE.md`) violation, even though it was the project's own
keys visible only to the operator. Any implementation here must fetch this
endpoint and immediately discard `.value`, checking only for **key
presence** (and non-empty-ness, which the API response can tell you without
ever surfacing the value itself in a log line or check output) — never
print or log the fetched values.

## What needs deciding (for the brainstorm, not decided here)

- **Should a new check exist** (e.g. `provider-live`, or `provider` itself
  extended) that, when `RENDER_API_KEY` is available, confirms the actively
  resolved provider's credential is genuinely present on the deployed
  service — not just locally? This is the most direct fix for the exact gap
  hit this session.
- **Should this run by default, or only opt-in** (mirroring how
  `render-service`/`uptime-pinger` already degrade to `SKIPPED` without
  their respective keys, per `config.py`'s stated rule: "absence degrades a
  check to SKIPPED, never to an error")? This new check would need
  `RENDER_API_KEY` specifically (not `DATABASE_URL`, which the existing
  `provider` check already uses to resolve the override itself).
- **Scope question:** does this only matter for the DB-override path (where
  the live provider can differ from `LLM_PROVIDER`), or should the *plain*
  env-configured case also get this treatment — i.e. should `config` itself
  eventually also verify against Render's live environment rather than only
  local `.env`, closing the same class of gap for the no-override case too?
  (Today `config` intentionally only checks local `.env`/environment — see
  §2.1 of the linked design — so broadening its scope is a bigger, separate
  decision from just adding a narrow live-provider-credential check.)
- **Whether `--sync-env` should be nudged/suggested automatically** when
  this new check fails, versus just reporting the gap and letting the
  operator decide (matching this CLI's existing "report, don't act
  unprompted" philosophy elsewhere, e.g. `github-app` only *writes* a
  corrected webhook URL, it doesn't ask first — worth checking whether that
  precedent argues for or against auto-suggesting `--sync-env` here too).
- **Naming/reporting shape:** how this new check's PASS/FAIL/SKIPPED rows
  should read, consistent with the existing table's style (see
  `README.md`'s "Verifying a deployment" table for the current seven checks'
  format).

## Suggested prompt to continue planning

> Brainstorm and then write an implementation plan for closing a real gap in
> `scripts/deploy.py`'s verification checklist: neither `check_config` nor
> `provider` can detect when the actively-resolved LLM provider's credential
> was never actually pushed to the live Render service — both checks
> validate local `.env` only, by design, and this was confirmed live during
> a demo rehearsal (switching to Gemini via the DB override reported clean
> on every check, then failed every real review because `GEMINI_API_KEY`
> had never been pushed to Render). Read
> `docs/2026-08-10-deploy-provider-credential-verification-gap.md` in full
> first — it has the complete context, confirms `GET
> /v1/services/{id}/env-vars` (via the existing `RENDER_API_KEY`) can
> answer this for real, flags a secrets-hygiene requirement for whatever
> implementation results (check key presence only, never log/print
> fetched values), and lists the open design questions (new check vs.
> extending `provider`; opt-in via `RENDER_API_KEY` presence, matching this
> CLI's existing SKIPPED-on-absence convention; whether `config` itself
> should eventually get the same treatment for the no-override case; whether
> to nudge `--sync-env` automatically on failure). Follow this repo's usual
> brainstorm → spec → plan conventions — `docs/superpowers/specs/2026-08-08-provider-agnostic-config-and-deploy-hardening-design.md`
> and its plan are good references for the expected rigor, since this is
> extending exactly the feature they shipped.
