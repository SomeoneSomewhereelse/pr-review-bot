# Step 2: Create the GitHub App

The GitHub App is the bot's identity: it's what lets the service read a
pull request's diff and post (and later edit) a review comment.

## The one-command path

```bash
uv run python -m scripts.create_github_app --name your-app-name
```

Run this yourself — it writes real credentials to `.env`, so it must never
be run by an agent.

`--name` defaults to `pr-review-engine`, but GitHub App names are globally
unique, so the exact default will already be taken if anyone else has run
this — pick your own.

You don't need a `--base-url` yet. This early, there's no real one to give
it: the tunnel (Local track) or Render URL (Hosted track) isn't created
until Step 6. Leave the flag off and the script falls back to the
obviously-fake `https://example.invalid` rather than silently creating a
webhook pointed nowhere useful — Step 7 (`scripts/deploy.py`) corrects the
webhook URL automatically once your real one exists. Only pass `--base-url`
yourself if you already have a stable URL at this point (e.g. a named
tunnel or a Render service you set up earlier).

This drives GitHub's **App Manifest flow**: it opens a browser form that
POSTs a manifest to `github.com/settings/apps/new`, you approve it, GitHub
redirects back with a one-time code, and the script exchanges that code for
the App ID, private key, and webhook secret — all in one round trip. That
replaces creating the App by hand, generating a private key by hand, and
base64-encoding it by hand.

!!! warning "Running this over SSH, on a headless box, or any remote session?"
    The default flow above needs the browser that approves the App to reach
    `localhost` **on the same machine running this command** — it opens a
    tiny local server and waits for GitHub to redirect back to it. If your
    terminal and your browser aren't on the same machine (SSH into a server,
    a cloud VM, a container with no GUI), that silently doesn't work: no
    browser opens, or the wrong machine's browser opens, and the command
    just blocks for up to 5 minutes before timing out with no explanation.

    Which fix applies depends on whether your browser's device shares a
    network with the machine running this command:

    **Same network (Tailscale, a VPN, the same LAN)** — e.g. you're SSH'd
    into a box from a phone or laptop that's on the same tailnet. Use
    `--bind-host` with an address of that machine your device can actually
    reach (a Tailscale IP/hostname, a LAN IP):

    ```bash
    uv run python -m scripts.create_github_app --name your-app-name --bind-host 100.x.y.z
    ```

    This stays fully automatic — no copy-pasting. Open the URL it prints
    (`http://100.x.y.z:8765/`) in your own device's browser, approve the App,
    and the rest happens exactly like the same-machine case. (The same
    result with zero extra flags: set up SSH local port forwarding —
    `ssh -L 8765:localhost:8765 that-host`, or Termius's own port-forwarding
    option — which makes the remote's `localhost:8765` answer as *your*
    device's `localhost:8765`, so the plain command with no flags at all
    already works.)

    **No shared network at all** (an isolated remote host — a bare cloud VM
    with only SSH exposed, nothing your device can reach directly) —
    `--bind-host` can't help here; there's no network path for it to use.
    Add `--manual` instead:

    ```bash
    uv run python -m scripts.create_github_app --name your-app-name --manual
    ```

    This needs no local browser and no localhost access at all. It writes a
    small HTML file and prints its path — copy that file to *any* machine
    that has a browser (`scp` it to your laptop or phone, AirDrop, anything)
    and open it there; it submits itself and takes you to GitHub's approval
    page exactly like the automatic flow does. After you approve, GitHub
    redirects to a page that will fail to load — that's expected, it's a
    placeholder with nothing behind it. Copy the full URL from your
    browser's address bar at that point (it carries a one-time code) and
    paste it back into the terminal running the command, which is waiting
    for exactly that. The rest — exchanging the code, writing `.env` —
    happens exactly as it does in the automatic flow.

The App is created with:

- **Permissions**: `pull_requests: write`, `contents: read`, `issues: write`,
  `metadata: read`.
- **Event**: `pull_request`.
- **Webhook URL**: a placeholder at creation time — the tunnel or Render URL
  doesn't exist yet. Step 7 (`scripts/deploy.py`) corrects it once your
  public URL is known.

!!! warning "Keep the App private"
    Do not publish the App to the GitHub Marketplace. Leaving
    `GITHUB_TARGET_REPO` unset (step 3) makes the bot act on *every* repo the
    installation covers, and that's only a safe default because only
    accounts *you* choose can install a private App in the first place. A
    public App would let any third party self-install and have their events
    accepted in that same track-all mode.

## Creating the App entirely by hand

This is a *different* fallback from `--manual` above — that flag still runs
the script and still gets you real credentials automatically once you paste
the code back; this path uses none of the script at all, doing every step
directly in GitHub's UI. Reach for this only if you'd genuinely rather not
run the script (e.g. you don't trust an unfamiliar script with `.env`, or
you're troubleshooting something the script itself is doing wrong) — for the
no-local-browser problem specifically, `--manual` above is less work.

If you don't already have a `.env` (the one-command path above creates it
for you; this path doesn't), start from the committed template:

```bash
cp .env.example .env
```

Then create the App by hand in GitHub's UI (**Settings → Developer settings
→ GitHub Apps → New GitHub App**), giving it the same permissions and event
listed above.

The form has its own **Webhook secret** field — unlike the one-command path,
GitHub does not generate this for you here, so type in your own value (any
random string) and copy that same value into `GITHUB_WEBHOOK_SECRET` in
`.env`. It's easy to skip since nothing about the App's settings *page*
prompts for it afterward, but the service refuses to start without it — it's
one of the four required boot credentials.

Then collect three IDs from the App's settings page — only two of which this
project uses:

- **App ID** → `GITHUB_APP_ID`. A short integer, near the top of the App's
  **General** settings page.
- **Installation ID** → `GITHUB_APP_INSTALLATION_ID`. **Required** no matter
  how the App was created — never auto-discovered or guessed on your behalf.
  It only exists once the App is installed on an account, so the next step
  covers how to capture it.
- **Client ID** — sits on the same settings page, and is easy to grab by
  mistake, but this project **does not use it at all**.

Download the private key as a `.pem` file, then base64-encode it with this
project's own script — never a raw file path:

```bash
uv run python -m scripts.encode_credential github-app-private-key.pem
```

Paste the output into `GITHUB_APP_PRIVATE_KEY` in `.env` (verbatim, the
whole base64 string — never a file path).

## Next

Continue to [Step 3: install the App](03-install-app.md) — GitHub does not
let an App install itself, so that part is always a browser step.
