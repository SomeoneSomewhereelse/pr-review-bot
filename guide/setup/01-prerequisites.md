# Step 1: Install prerequisites

The goal of this step is to get the checkout running and its test suite
green before touching any credentials — that proves the toolchain works
before anything harder is layered on top. Get everything below in place
*first*, then run the test suite at the end of this page — running it any
earlier just means hitting an avoidable failure.

## What you need

| Tool | Why | Check |
|---|---|---|
| **Python 3.12** | pinned in `.python-version` | see below |
| [**uv**](https://docs.astral.sh/uv/) | this project's package/venv manager | `uv --version` |
| **git** | to clone the repo | `git --version` |
| **Docker, *or* a reachable `DATABASE_URL`** | the test suite and the app both need a real Postgres | see below |

Checking your Python version:

=== "Linux"

    ```bash
    python3 --version
    ```

=== "macOS"

    ```bash
    python3 --version
    ```

=== "Windows"

    ```powershell
    py --version
    ```

The Docker-or-`DATABASE_URL` line is one conditional, not two separate
requirements: `tests/conftest.py`'s `db_url` fixture spins up a throwaway
Postgres 16 container via `testcontainers` automatically when Docker is
present, or reuses a `DATABASE_URL` you already point at a reachable
local/CI Postgres. Without *either* one, the DB-touching tests fail with an
opaque testcontainers error that doesn't say "install Docker" — so get one
of the two in place before running the test suite below.

## Installing Docker

Skip this if you already have a `DATABASE_URL` pointing at a Postgres you
can reach — Docker is only one way to satisfy that prerequisite, not the
requirement itself.

=== "Linux"

    ```bash
    sudo apt install docker.io
    ```

    Then add yourself to the `docker` group. Official install page:
    <https://docs.docker.com/get-docker/>

=== "macOS"

    ```bash
    brew install --cask docker
    ```

    Official install page: <https://docs.docker.com/get-docker/>

=== "Windows"

    ```powershell
    winget install Docker.DockerDesktop
    ```

    Official install page: <https://docs.docker.com/get-docker/>

## Get the checkout running

With Python, uv, git, and Docker (or a `DATABASE_URL`) all in place:

```bash
git clone <your-fork-or-clone-url>   # e.g. https://github.com/<you>/pr-review-bot.git
cd pr-review-bot   # or whatever your clone created
uv sync
uv run pytest
```

`<your-fork-or-clone-url>` is whatever URL you're getting this project's
source from — your own fork if you plan to push changes anywhere, or a
direct clone of the upstream repo otherwise; either works equally well for
running the bot.

If `uv run pytest` passes, the checkout is sound and every tool above is
correctly in place.

## Next

```bash
uv run python -m scripts.doctor
```

Run it now — it checks every prerequisite above (and everything in the
steps that follow) and tells you exactly what's still missing.
