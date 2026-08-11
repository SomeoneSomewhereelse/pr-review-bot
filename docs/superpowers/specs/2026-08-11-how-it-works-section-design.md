# Design: "How It Works" Dashboard Section

Date: 2026-08-11

## Purpose

A visually distinct section below the review list on `/dashboard` that
explains, at a high level, how the review pipeline works — for a viewer
who has never seen the system before (demo audience, grader). Purely
explanatory; no live data, no interactivity beyond what's needed for
theme/language, which it inherits from the rest of the page.

## Scope

High-level flow only — the user-facing sequence of what happens to a PR,
not the underlying architecture (no webhook HMAC verification, durable
queue, dispatcher, provider names, or cost model — those live in `SPEC.md`
and `cost.md` for readers who want that depth).

## Content

Five steps, the third being a parallel group of three specialists. All
copy is UI chrome translated via the existing `STRINGS` object (same
pattern as the rest of the page) — none of it is LLM-generated, so the
finding-content translation boundary from the main dashboard doesn't apply
here.

| # | Icon | Title (en) | Description (en) |
|---|------|------------|-------------------|
| 1 | 🔔 | PR opened or updated | A pull request is opened, reopened, or pushed to. |
| 2 | 📄 | Diff fetched | The bot fetches the changed code and annotates it with file:line references. |
| 3 | (group) | 3 specialists review in parallel | — |
| 3a | 🔒 | Security | Reviews the diff independently. |
| 3b | ⚡ | Performance | Reviews the diff independently. |
| 3c | 🧹 | Code Quality | Reviews the diff independently. |
| 4 | 🧩 | Findings merged | Results from all three specialists are combined into one report — even if one fails, the others still show. |
| 5 | 💬 | Comment posted | A single comment appears on the PR with every finding. |

The three specialist icons (🔒⚡🧹) are the same ones already used in
`app/formatting.py`'s `_SECTION_CONFIG` for the PR comment's own section
headers — reusing them here keeps the visual language consistent between
the dashboard and the actual PR comment a viewer might go look at next.

Hebrew translations for all of the above go into `STRINGS.he` following the
same key-naming convention as the rest of the page (e.g. `hiw_step1_title`,
`hiw_step1_desc`, `hiw_heading`, `hiw_parallel_label`, `hiw_step3a_title`,
etc. — `hiw` prefix for "how it works", to keep these keys visually grouped
and distinct from the dashboard's own `stat_*`/`col_*`/`q_*` keys).

## Layout

- **Wide screens (≥1000px):** a horizontal flex row of 5 "slots" — step 1,
  step 2, the parallel group, step 4, step 5 — connected by arrow
  connectors. The parallel group is itself a small bordered/tinted
  container holding the 3 specialist mini-cards side-by-side. (This
  breakpoint was corrected from an original ≥760px intent — the row's own
  `min-width` floors on `.hiw-step`/`.hiw-parallel-group` plus gaps/arrows
  add up to ~966px minimum, so 760px was never actually achievable; the
  row layout now turns on only once the viewport can actually fit it.)
- **Narrow screens (<1000px):** the same flex row becomes a vertical
  column (steps stacked top-to-bottom); the parallel group's 3 mini-cards
  also stack vertically inside their container below 760px. The outer
  flow and inner group intentionally use different breakpoints here: the
  outer flow's breakpoint had to move to 1000px to fix the row's minimum-
  width overflow, but the inner group's 3 mini-cards stack fine down to
  760px on their own, so its breakpoint stayed put.
- **Connectors:** a single arrow glyph between each adjacent slot (including
  one arrow into the parallel group and one arrow out of it — never three
  individual branching lines to/from each specialist card, which would need
  much more layout work for no added clarity).
  - At the wide breakpoint, the arrow points in the flow's horizontal
    direction and is mirrored via `[dir="rtl"] { transform: scaleX(-1); }`
    — the same directional-mirroring approach RTL text itself uses, so the
    arrow always points toward where the *next* step visually sits.
  - At the narrow breakpoint, the arrow instead points downward via a
    fixed `rotate(90deg)`, with **no RTL-specific variant** — vertical
    "down" doesn't flip with reading direction, so this single rule
    covers both languages (unlike the horizontal case). This mirrors the
    lesson from the review-list's expand chevron, which only ever needed
    a vertical rotation, never a horizontal mirror.
- **Container width:** stays within the page's existing centered
  `max-width: 1100px` content column — no full-bleed edge-to-edge band,
  avoiding the overflow/horizontal-scroll risk class of bug already fixed
  once on this page.

## Visual treatment

- The section's outer container gets a `var(--surface-2)` background (the
  same "sunken" tone already used for `#errorBanner`), a `1px solid
  var(--border)` border, and rounded corners — visually distinct from the
  `var(--surface)` cards used by stat tiles and review rows above it,
  without introducing a new color.
- A heading ("How it works" / "איך זה עובד") sits above the flow, styled
  like the page's existing `<h1>` but one size down.
- Each step is a small card: icon, title, one-line description — matching
  the existing `.stat-tile`/`.review-card` visual language (surface color,
  border, radius) rather than inventing a new card style.
- The parallel group's container gets a subtle accent-tinted border (e.g.
  `var(--accent)` at low weight) to visually separate "these three happen
  together" from "these are two more sequential steps."

## Placement

A new `<section id="howItWorks">` appended inside `<main>`, directly after
the existing `#reviews` section. It renders once from static translated
strings (no `/api/dashboard` data involved) — `applyLanguage()` re-renders
its text the same way it already does for every other `data-i18n` element
and radio/popup label, via the existing mechanism. No new poll, no new
fetch, no new backend endpoint.

## Testing

Same convention already established for this static page (no JS test
runner in this repo): `pytest` assertions over the served HTML string in
`tests/test_dashboard_page.py`, checking for:
- the heading and all five step titles/descriptions in both `STRINGS.en`
  and `STRINGS.he`,
- the parallel-group container and its three specialist mini-cards,
- the arrow-connector class and its RTL-mirror rule existing in the CSS,
- the narrow-breakpoint vertical-rotation rule existing in the CSS.

Actual responsive layout (row-to-column collapse) and RTL arrow mirroring
get a manual browser check, the same way Tasks 5–6 of the original
dashboard plan were verified — this repo has no visual/browser test
infrastructure to assert pixel layout automatically.

## Out of scope (YAGNI)

- Any live data — this section never touches `/api/dashboard`.
- Animation/scroll-triggered reveal effects.
- A "read more" link into `SPEC.md`/`cost.md` (nothing was asked for this;
  can be added later if wanted).
- Per-specialist links into the actual code of `app/specialists/*.py` —
  purely descriptive, not interactive.
