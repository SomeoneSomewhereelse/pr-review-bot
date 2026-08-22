# Step 6: Start a tunnel

## Why a tunnel at all

GitHub delivers webhook events to a public HTTPS URL. Without one, GitHub
never reaches your machine, so a PR event is never delivered and the actual
trigger — the thing that makes this a webhook-driven bot rather than a
script you run by hand — is never exercised. A tunnel is what makes
`localhost:8000` reachable from GitHub's side.

## Optional milestone: verify the pipeline before investing in the tunnel

If you want proof the App auth, diff-fetch, and comment-upsert pipeline
works *before* setting up a tunnel, this is optional but worth doing now,
before installing anything below:

```bash
uv run python -m scripts.manual_verify_step3
```

It proves the pipeline against a real PR, with **no public-URL dependency**
at all. It does not prove the *trigger* (GitHub delivering the webhook) —
only steps 6–7 do that — so treat it as a confidence check, not a
substitute for the tunnel.

## Install it

`cloudflared` isn't in Step 1's shared prerequisites because it's only
needed on this track. Install it, then confirm with `cloudflared --version`:

=== "Linux"

    ```bash
    sudo apt install cloudflared   # or download the binary
    ```

=== "macOS"

    ```bash
    brew install cloudflared
    ```

=== "Windows"

    ```powershell
    winget install Cloudflare.cloudflared
    ```

Official download page: <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/>.
`uv run python -m scripts.doctor` also checks for it and prints the same
install hint if it's missing.

## Start one

In a **second terminal** (the first will run the service in step 8):

```bash
cloudflared tunnel --url http://localhost:8000
```

`cloudflared`'s quick tunnel (TryCloudflare) is the documented default here,
not a hard dependency — it is the only option needing no account, no config,
one binary, and one command. ngrok now requires a free account and an
authtoken; Tailscale Funnel and VS Code port forwarding both need accounts
too. Any tool that yields a public HTTPS URL works — all the app needs is
that URL in `PUBLIC_BASE_URL` plus a registered webhook (next step).

## Set it

`cloudflared` prints an `https://<random>.trycloudflare.com` URL to its
terminal. Set that as `PUBLIC_BASE_URL` in `.env`.

!!! warning "The URL changes on every restart"
    A quick tunnel's URL is ephemeral — it changes every time you restart
    `cloudflared`, which means `PUBLIC_BASE_URL` changes too, and step 7
    (register the webhook) has to be re-run each session to point GitHub at
    the new URL. A **named** Cloudflare tunnel gives a stable hostname
    instead, but it needs a Cloudflare account and DNS configuration — out
    of scope for this track.

## Next

Continue to [Step 7: register the webhook](07-webhook.md).
