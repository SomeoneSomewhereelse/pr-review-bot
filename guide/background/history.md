# Repo history and redo-from-scratch notes

## Repo history note

This repo was extracted from a course repository (Tov-learn), where it lived
at `study/final_project/` on branch `feat/project-d-code-review-engine`, via
`git subtree split` — full commit history preserved, paths rewritten
relative to this repo's root. The course repo's copy (and that branch) still
exist independently; this is now the standalone home going forward.

## Redo-from-scratch notes

If any of this needs to be redone (e.g. rotating the webhook secret, a new
PEM):

- GitHub App settings: `https://github.com/settings/apps/<your-app-slug>`
- Test repo: `https://github.com/<your-user>/pr-review-bot-testbed`
- Gemini key management: `https://aistudio.google.com/app/apikey`

For the actual redo steps themselves — creating a new GitHub App, encoding a
new PEM, registering a new webhook — see the [setup guide](../setup/index.md)
rather than this page; those instructions live there so they stay in one
place as the tooling evolves.
