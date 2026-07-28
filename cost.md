# cost.md — Operating Cost

> All rates are **representative** and pinned in `app/providers/pricing.py`; verify
> against current provider pricing at build time. `gemini-flash-latest` = Gemini 3.5
> Flash. Cost is graded as this documented calculation, not as actual spend — the
> demo runs at **$0** on free tiers + the $300 GCP trial credit.

## 1. Deploy-approach comparison

| Approach | Infra $/mo | Cold start | Full stack native? | Demo fit | Notes |
|---|---|---|---|---|---|
| **Container + Cloudflare Tunnel** (chosen) | **$0** (local/free host + free tunnel) | none (kept running) | ✅ FastAPI + PyGitHub + Vertex SDK | ✅ best | Public URL via `cloudflared`; portable to CF Container unchanged |
| Cloudflare Container | ~$5 (Workers Paid base; scales to zero) | few seconds after idle | ✅ | ✅ (pre-warm) | Matches "Workers" checkbox literally; only real cost is the $5 base |
| Cloudflare Python Workers (Pyodide) | $0–5 | minimal | ❌ Vertex SDK won't run; PyGitHub risky | ❌ | Most literal "Workers" but breaks the mandated stack (SDK → REST) |
| Render (Starter, always-on) | $7 | none | ✅ | ✅ | Zero-setup always-warm; weakest match to "Workers" wording |
| Render (Free, spin-down) | $0 | ~60s after 15 min idle | ✅ | ⚠️ risky | Free public URL, but cold start can blow the 15s demo budget |

**Choice rationale:** the chosen path is $0, keeps the full stack native (no Pyodide
compromises), gives a real public URL, and is portable to the $5 CF Container later
with zero code change if the literal "Workers" checkbox is wanted.

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

- **Vertex** on the **$300 GCP trial credit** (90 days) covers all LLM calls
  when enabled (not configured in this environment — see `SETUP.md`).
- **Gemini** AI-Studio free tier: ~1,500 req/day, no card — permanent $0
  fallback in principle (account-blocked in this environment, see `SETUP.md`).
- **Groq** free tier: ~30 RPM / up to 14.4K req/day — the actual live
  provider, independent $0 path.
- **GitHub Models** free tier: rides the user's GitHub account, no card —
  a second genuinely live $0 cross-vendor path (modest RPM/RPD, see `SETUP.md`).
- **Host**: local/free host + **free Cloudflare Tunnel** → $0 public URL.
- **CI**: GitHub Actions — $0 on the free tier for this workload.

## 5. Durable review queue — no cost impact

The durable review queue (`SPEC.md` §12) changes **when** LLM calls happen
(serialized through one dispatcher, deferred/retried on a `429`), not **how
many** are made per review — the per-review token math in section 2 is
unchanged. SQLite ticket persistence is embedded (stdlib `sqlite3`, a local
file), so it adds **$0** infra cost.
