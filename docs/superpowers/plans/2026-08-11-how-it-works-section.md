# "How It Works" Dashboard Section Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a visually distinct, translated, RTL-correct "How it works" explainer section below the review list on `/dashboard`, showing the high-level review pipeline as a 5-slot flow (the third slot being a bracketed group of 3 parallel specialist mini-cards) that collapses from a horizontal row to a vertical stack on narrow screens.

**Architecture:** Purely additive to the existing static page — new HTML markup, CSS, and `STRINGS` entries in `app/static/dashboard.html`. No new backend endpoint, no new fetch, no new render function: the section's text is static and picked up automatically by the page's existing `applyLanguage()` mechanism (it already walks every `[data-i18n]` element on every language switch).

**Tech Stack:** Same as the rest of the page — hand-rolled HTML/CSS/JS, no framework, no build step.

## Global Constraints

- No new Python dependency, no CDN script, no new backend endpoint or fetch — this section never touches `/api/dashboard` (design spec, "Placement").
- All copy is UI chrome, translated via the existing `STRINGS` object — none of it is LLM-generated (design spec, "Content").
- The whole flow is vertical top-to-bottom on narrow screens and switches to horizontal on wide screens (≥761px / <761px, one breakpoint drives both the outer flow and the inner parallel-group's 3 mini-cards) (design spec, "Layout").
- Arrow connectors point in the flow direction: horizontally mirrored under `dir="rtl"` **only at the wide breakpoint**; rotated 90° downward at the narrow breakpoint **unconditionally** (no RTL variant there — vertical "down" doesn't flip with reading direction) (design spec, "Layout" — this is the same lesson already learned from the review-list's expand chevron).
- The section stays within the page's existing centered `max-width: 1100px` content column — no full-bleed band (design spec, "Container width").
- Testing follows the page's existing convention: `pytest` string assertions over the served HTML in `tests/test_dashboard_page.py`; actual responsive/RTL rendering gets a manual browser check (design spec, "Testing").

---

### Task 1: Add the "How it works" section

**Files:**
- Modify: `app/static/dashboard.html` (CSS additions, new `<section>`, `STRINGS` additions)
- Test: `tests/test_dashboard_page.py` (extend)

**Interfaces:**
- Consumes: the existing `applyLanguage()` mechanism (`app/static/dashboard.html:310-324`), which already re-renders every `[data-i18n]` element's `textContent` from `STRINGS[currentLang]` on every language switch and on page load — no changes needed there. Also reuses the existing `sp_name_security`/`sp_name_performance`/`sp_name_quality` `STRINGS` keys (`:245-246`, `:272-273`) for the three specialist mini-card titles, rather than introducing duplicate translation strings for the same three names — the design spec's key-naming examples (`hiw_step3a_title` etc.) were illustrative, not mandatory; reusing the existing keys keeps the specialist names' translation in exactly one place.
- Produces: no new JS functions, no new IDs other than `#howItWorks` (not referenced by any other task or file).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_dashboard_page.py`:

```python
async def test_dashboard_page_has_how_it_works_section():
    """The explainer section: heading, all step copy in both languages, the
    parallel-group container and its mini-cards, and the arrow connector's
    RTL-mirror (wide screens only) / rotation (narrow screens, unconditional)
    rules."""
    client = await _client()
    resp = await client.get("/dashboard")
    body = resp.text
    assert 'id="howItWorks"' in body
    assert "hiw_heading" in body
    assert "How it works" in body
    assert "איך זה עובד" in body
    assert "hiw_parallel_label" in body
    assert "3 specialists review in parallel" in body
    assert "3 מומחים בודקים במקביל" in body
    assert "hiw-parallel-group" in body
    assert "hiw-mini-card" in body
    assert "hiw-arrow" in body
    assert "scaleX(-1)" in body
    assert "rotate(90deg)" in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_dashboard_page.py -k how_it_works -v`
Expected: FAIL — none of `id="howItWorks"`, `hiw_heading`, etc. exist in the page yet.

- [ ] **Step 3: Add the CSS**

In `app/static/dashboard.html`, insert the following immediately before the closing `</style>` tag (i.e. right after the existing `a.comment-link { color: var(--accent); text-decoration: none; }` rule):

```css
  .how-it-works {
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 0.6rem;
    padding: 1.25rem 1rem;
    margin-top: 1.5rem;
  }
  .how-it-works h2 { font-size: 1.1rem; margin: 0 0 1rem; }
  .hiw-flow {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
  }
  @media (max-width: 760px) {
    .hiw-flow { flex-direction: column; }
  }
  .hiw-step {
    flex: 1;
    min-width: 140px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 0.6rem;
    padding: 0.75rem;
    text-align: center;
  }
  .hiw-icon { font-size: 1.6rem; }
  .hiw-title { font-weight: 600; font-size: 0.9rem; margin-top: 0.3rem; }
  .hiw-desc { color: var(--text-muted); font-size: 0.8rem; margin-top: 0.2rem; }
  .hiw-arrow {
    align-self: center;
    flex: 0 0 auto;
    width: 1.4rem;
    height: 1.4rem;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
    font-size: 1.2rem;
  }
  .hiw-arrow::before { content: "→"; display: inline-block; }
  @media (min-width: 761px) {
    [dir="rtl"] .hiw-arrow::before { transform: scaleX(-1); }
  }
  @media (max-width: 760px) {
    .hiw-arrow::before { transform: rotate(90deg); }
  }
  .hiw-parallel-group {
    flex: 1.6;
    min-width: 220px;
    background: var(--surface);
    border: 1px solid var(--accent);
    border-radius: 0.6rem;
    padding: 0.75rem;
  }
  .hiw-parallel-label {
    text-align: center;
    font-weight: 600;
    font-size: 0.85rem;
    margin-bottom: 0.5rem;
    color: var(--accent);
  }
  .hiw-parallel-cards {
    display: flex;
    gap: 0.5rem;
  }
  @media (max-width: 760px) {
    .hiw-parallel-cards { flex-direction: column; }
  }
  .hiw-mini-card {
    flex: 1;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 0.5rem;
    padding: 0.5rem;
    text-align: center;
  }
  .hiw-mini-card .hiw-icon { font-size: 1.3rem; }
  .hiw-mini-card .hiw-title { font-size: 0.85rem; }
  .hiw-mini-card .hiw-desc { font-size: 0.75rem; }
```

Note on the arrow rules: the RTL mirror lives *only* inside the
`(min-width: 761px)` query and the rotation rule *only* inside the
`(max-width: 760px)` query, with no `[dir="rtl"]` scoping on the rotation
rule. This is deliberate — putting the mirror rule inside an unconditional
selector would make it win by specificity over the rotation rule at narrow
RTL widths (a `[dir="rtl"] .hiw-arrow::before` selector is more specific
than a bare `.hiw-arrow::before` one, regardless of which media query
either lives in), silently breaking the mobile arrow the same way the
review-list chevron broke before its fix. Keeping the two rules in
mutually exclusive media queries (`min-width: 761px` vs `max-width: 760px`)
sidesteps the specificity question entirely: only one of the two rules
can ever be in effect at a given viewport width.

- [ ] **Step 4: Add the section markup**

In `app/static/dashboard.html`, insert the following inside `<main>`,
directly after the existing `<section id="reviews"></section>` line (and
before the closing `</main>` tag):

```html
    <section id="howItWorks" class="how-it-works">
      <h2 data-i18n="hiw_heading"></h2>
      <div class="hiw-flow">
        <div class="hiw-step">
          <div class="hiw-icon">🔔</div>
          <div class="hiw-title" data-i18n="hiw_step1_title"></div>
          <div class="hiw-desc" data-i18n="hiw_step1_desc"></div>
        </div>
        <div class="hiw-arrow" aria-hidden="true"></div>
        <div class="hiw-step">
          <div class="hiw-icon">📄</div>
          <div class="hiw-title" data-i18n="hiw_step2_title"></div>
          <div class="hiw-desc" data-i18n="hiw_step2_desc"></div>
        </div>
        <div class="hiw-arrow" aria-hidden="true"></div>
        <div class="hiw-parallel-group">
          <div class="hiw-parallel-label" data-i18n="hiw_parallel_label"></div>
          <div class="hiw-parallel-cards">
            <div class="hiw-mini-card">
              <div class="hiw-icon">🔒</div>
              <div class="hiw-title" data-i18n="sp_name_security"></div>
              <div class="hiw-desc" data-i18n="hiw_step3_desc"></div>
            </div>
            <div class="hiw-mini-card">
              <div class="hiw-icon">⚡</div>
              <div class="hiw-title" data-i18n="sp_name_performance"></div>
              <div class="hiw-desc" data-i18n="hiw_step3_desc"></div>
            </div>
            <div class="hiw-mini-card">
              <div class="hiw-icon">🧹</div>
              <div class="hiw-title" data-i18n="sp_name_quality"></div>
              <div class="hiw-desc" data-i18n="hiw_step3_desc"></div>
            </div>
          </div>
        </div>
        <div class="hiw-arrow" aria-hidden="true"></div>
        <div class="hiw-step">
          <div class="hiw-icon">🧩</div>
          <div class="hiw-title" data-i18n="hiw_step4_title"></div>
          <div class="hiw-desc" data-i18n="hiw_step4_desc"></div>
        </div>
        <div class="hiw-arrow" aria-hidden="true"></div>
        <div class="hiw-step">
          <div class="hiw-icon">💬</div>
          <div class="hiw-title" data-i18n="hiw_step5_title"></div>
          <div class="hiw-desc" data-i18n="hiw_step5_desc"></div>
        </div>
      </div>
    </section>
```

- [ ] **Step 5: Add the `STRINGS` entries**

In `app/static/dashboard.html`, add the following keys to `STRINGS.en`
(anywhere in the object — e.g. right after `backoff_none: "none",`):

```javascript
        hiw_heading: "How it works",
        hiw_step1_title: "PR opened or updated",
        hiw_step1_desc: "A pull request is opened, reopened, or pushed to.",
        hiw_step2_title: "Diff fetched",
        hiw_step2_desc: "The bot fetches the changed code and annotates it with file:line references.",
        hiw_parallel_label: "3 specialists review in parallel",
        hiw_step3_desc: "Reviews the diff independently.",
        hiw_step4_title: "Findings merged",
        hiw_step4_desc: "Results from all three specialists are combined into one report — even if one fails, the others still show.",
        hiw_step5_title: "Comment posted",
        hiw_step5_desc: "A single comment appears on the PR with every finding.",
```

And the matching keys to `STRINGS.he` (right after `backoff_none: "אין",`):

```javascript
        hiw_heading: "איך זה עובד",
        hiw_step1_title: "PR נפתח או עודכן",
        hiw_step1_desc: "בקשת משיכה (PR) נפתחת, נפתחת מחדש, או מתעדכנת בקוד.",
        hiw_step2_title: "שליפת ה-diff",
        hiw_step2_desc: "הבוט שולף את הקוד שהשתנה ומסמן אותו לפי קובץ ומספר שורה.",
        hiw_parallel_label: "3 מומחים בודקים במקביל",
        hiw_step3_desc: "בודק את ה-diff באופן עצמאי.",
        hiw_step4_title: "איחוד הממצאים",
        hiw_step4_desc: "התוצאות משלושת המומחים מאוחדות לדוח אחד — גם אם אחד נכשל, האחרים עדיין מוצגים.",
        hiw_step5_title: "פרסום תגובה",
        hiw_step5_desc: "תגובה אחת מתפרסמת ב-PR עם כל הממצאים.",
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `pytest tests/test_dashboard_page.py -v`
Expected: PASS (all tests in the file, including the new one).

- [ ] **Step 7: Run the full test suite**

Run: `pytest -v`
Expected: PASS — everything, no regressions.

- [ ] **Step 8: Manually verify in a browser**

Run: `uv run uvicorn app.main:app --reload` (per `SETUP.md`, or stub
`store.init_pool`/skip the lifespan if just eyeballing the static page),
then open `http://localhost:8000/dashboard` and confirm:
- The new section sits below the review list, visually distinct from it
  (tinted background, its own heading), and stays within the same centered
  content width as the rest of the page.
- At a wide viewport, the 5 slots run left-to-right with arrows pointing
  rightward between them, and the parallel group shows its 3 specialist
  mini-cards side-by-side with one arrow in and one arrow out.
- Resizing below ~760px collapses the flow to a vertical stack, the arrows
  rotate to point downward, and the parallel group's 3 mini-cards stack
  vertically too.
- Switching to עברית translates the heading, all five step titles/
  descriptions, and the parallel-group label; at a wide viewport the arrows
  now point leftward (mirrored); at a narrow viewport they still point
  downward (unchanged by language).

- [ ] **Step 9: Commit**

```bash
git add app/static/dashboard.html tests/test_dashboard_page.py
git commit -m "feat: add \"how it works\" explainer section to the dashboard"
```
