# Step 8: Add the keep-warm pinger

Both Render (free tier) and Supabase (free tier) spin down after inactivity.
A free external pinger keeps both warm.

## Create the monitor

1. Go to <https://uptimerobot.com> (free) — cron-job.org also works, but
   UptimeRobot is what `scripts/deploy.py`'s `uptime-pinger` check verifies
   against.
2. Create a new monitor that pings your Render URL's `/healthz` endpoint:

   ```
   https://<your-service>.onrender.com/healthz
   ```

3. Set the interval to **5 minutes**; anything above 10 lets Render's
   ~15-minute spin-down win.

This keeps Render warm and also keeps Supabase un-paused — the dispatcher
polls the queue continuously, so pinging `/healthz` guarantees activity.

!!! warning "The URL must match exactly"
    A stray trailing character (a comma pasted from prose, for instance)
    404s on every check while the dashboard still shows the monitor firing
    on schedule and looking perfectly healthy. Double-check the URL you
    pasted against the one above, character for character.

## Why `/healthz` answers both GET and HEAD

UptimeRobot's free tier sends `HEAD` rather than `GET`, which is why
`/healthz` answers both verbs.

## Optional: let doctor verify it

Set `UPTIMEROBOT_API_KEY` locally if you want `doctor`/`deploy` to verify
the monitor's existence and interval rather than report `SKIPPED`.

## Done

All eight steps are complete. `uv run python -m scripts.doctor` should now
report every row `PASS` (or `SKIP` where a credential like
`UPTIMEROBOT_API_KEY` was left unset by choice).
