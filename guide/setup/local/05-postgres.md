# Step 5: Get a Postgres

`store.py` is psycopg3-only, so a real reachable Postgres is a hard
requirement — there is no SQLite fallback. There are three ways to get one;
pick whichever is least friction for you.

=== "Docker (recommended)"

    ```bash
    docker run -d --name pr-review-pg -p 5432:5432 -e POSTGRES_PASSWORD=x postgres:16
    ```

    ```bash
    DATABASE_URL=postgresql://postgres:x@localhost:5432/postgres
    ```

=== "Native install"

    Install Postgres 16 directly from <https://www.postgresql.org/download/>
    and create a database, then point `DATABASE_URL` at it.

=== "Remote (e.g. Supabase, free tier)"

    A free [Supabase](https://supabase.com) project works fine here — this
    track uses it purely as a remote Postgres, with none of the Render
    coupling described on the hosted track.

    Open **Connect** (or Project Settings → Database) and copy the
    **Session-mode pooler** connection string — port **5432**, not 6543:

    ```
    postgresql://postgres.<project-ref>:<password>@aws-<region>.pooler.supabase.com:5432/postgres
    ```

    Copy it verbatim; do not retype or reconstruct it. Both the
    `postgres.<project-ref>` username and the region-varying subdomain are
    project-specific, and either one wrong yields `FATAL: Tenant or user not
    found`.

## Set it

Whichever option you picked, set the resulting connection string as
`DATABASE_URL` in `.env`.

## Next

```bash
uv run python -m bot.scripts.doctor
```

The `database` row turns `PASS` once `DATABASE_URL` is reachable. Then
continue to [Step 6: start a tunnel](06-tunnel.md).
