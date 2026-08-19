# Deploying an image, when the Render service has no connected repo

Render **always builds on Render** — either from a connected GitHub repo, or
by pulling a pre-built image from a container registry. It never accepts or
uploads your local working tree, in either mode.

If your Render service is configured against a registry image rather than a
repo:

1. Build locally: `docker build -t ghcr.io/<you>/pr-review-engine:<tag> .`
2. Push it to the registry: `docker push ghcr.io/<you>/pr-review-engine:<tag>`
3. In the Render dashboard, point the service at that image and tag
   (Settings).
4. Run `--sync-env` (see [Deploying and verifying](deploy.md)) to push config
   and trigger a deploy against the new image.

## How verification adapts

The `render-service` check reports whichever artifact is actually live — a
git commit sha for a repo-connected service, or the image ref for an
image-backed one — and only attempts the local-`HEAD` comparison **when a
commit is present**. An image-backed deploy reports `PASS` with "no local
comparison possible" rather than inventing a mismatch it has no way to
check. See [Deployment checks](../reference/checks.md) for the rest of the
check list.

## Next

- [Deploying and verifying](deploy.md)
- [Switching providers and API keys](overrides.md)
