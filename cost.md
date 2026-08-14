# cost.md — Operating Cost

> All rates are **representative** and pinned in `app/providers/pricing.py`; verify
> against current provider pricing at build time. `gemini-flash-latest` = Gemini 3.5
> Flash. Cost is graded as this documented calculation, not as actual spend — the
> demo runs at **$0** on free tiers + the $300 GCP trial credit.

## 1. Deploy-approach comparison

| Approach | Infra $/mo | Persistence | Public URL | Demo fit | Notes |
|---|---|---|---|---|---|
| **Render (Free) + Supabase (Free)** (chosen) | **$0** (free tiers + external pinger) | ✅ Postgres queue | ✅ stable | ✅ best | Render spins down after 15 min idle; kept warm by cron pinger (~10 min); Supabase pauses after ~7 days, mitigated by dispatcher polling |
| Render (Starter, always-on) | $7 | ✅ Postgres queue | ✅ stable | ✅ | Always warm; costs $7/mo infra alone |
| Cloudflare Container | ~$5 (Workers Paid base) | ❌ SQLite only (ephemeral) | ✅ stable | ✅ | Scales to zero but queue state lost on redeploy; would need separate DB |
| Local + Cloudflare Tunnel | $0 (if local machine kept on) | ✅ SQLite queue | ⚠️ unstable | ⚠️ only local | Tunnel URL changes every restart (no stable webhook); local machine must stay on |

**Choice rationale:** the chosen path is $0 on free tiers with stable public URLs
and durable queue state via Postgres. The keep-warm pinger (~$0, free service)
mitigates Render and Supabase idle spin-down, keeping both within the demo's 15s
responsiveness target. The two lower rows were evaluated and **rejected**, and
are listed here only to document that comparison: the Cloudflare Container loses
queue state on redeploy, and the local-machine-plus-tunnel setup — which this
project used before the Render migration — has no stable webhook URL and needs a
laptop kept awake. Neither is a supported deployment path today.

## 2. LLM cost (same across every deploy approach)

Per-specialist call ≈ **4K input + 500 output tokens**; 3 specialists per review.
Representative flash-class rates: **~$0.30 / 1M input, ~$2.50 / 1M output**.

- Per review: input 12K → $0.0036 · output 1.5K → $0.0038 · **≈ $0.0074 / review**
- **Brief scale (20 PRs/day):** 600 reviews/mo × $0.0074 ≈ **$4.4/mo** (matches the brief's $3–5)
- **`synchronize` effect:** ~2× reviews/PR with pushes → **≈ $9/mo**
- **Global scale (100 req/day):** 3,000 reviews/mo × $0.0074 ≈ **$22/mo**

## 3. Documented monthly total

| Scenario | Infra | LLM | **Total** |
|---|---|---|---|
| **Demo (chosen path, free tiers + $300 credit)** | $0 | $0 | **$0** |
| Brief scale (20 PRs/day), CF Container | $5 | $4–5 | **~$9–10** (matches brief) |
| Global scale (100 req/day), CF Container | $5 | ~$22 | **~$27** |

## 4. Free-tier headroom (why the demo is $0)

- **Vertex** on the **$300 GCP trial credit** (90 days) covers all LLM calls —
  implemented as of 2026-08-14 (`LLM_PROVIDER=vertex`) — code-complete and
  unit-tested, but live verification against a real GCP-billed project is
  still outstanding; see `SETUP.md` §2. Billed at the same per-token rate as
  the Gemini entry below (`app/providers/pricing.py`); the two differ in the
  auth path, not in price.
- **Gemini** AI-Studio free tier: ~1,500 req/day, no card — permanent $0
  fallback in principle (account-blocked in this environment, see `SETUP.md`).
- **Groq** free tier: ~30 RPM / up to 14.4K req/day — the actual live
  provider, independent $0 path.
- **GitHub Models** free tier: rides the user's GitHub account, no card —
  a second genuinely live $0 cross-vendor path (modest RPM/RPD, see `SETUP.md`).
- **Render** free tier: 750 instance-hours/month (15 min idle spin-down).
  Mitigated by keep-warm pinger (free service, cron-job.org / UptimeRobot).
- **Supabase** free tier: ~500 MB Postgres storage, pauses after ~7 days
  inactivity. Mitigated by dispatcher's continuous polling while kept warm
  by the pinger.
- **External pinger**: cron-job.org or UptimeRobot (both free tier).
- **CI**: GitHub Actions — $0 on the free tier for this workload.

## 5. Durable review queue — included in Supabase free tier

The durable review queue (`SPEC.md` §12) changes **when** LLM calls happen
(serialized through one dispatcher, deferred/retried on a `429`), not **how
many** are made per review — the per-review token math in section 2 is
unchanged. Postgres ticket persistence is now served by Supabase's free tier
(~500 MB storage), so queue state adds **$0** cost and is durable across
Render redeploys.
