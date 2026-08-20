# PR Review Engine

Open a pull request, and this bot reviews it automatically — checking for
security risks, performance issues, and code-quality problems — then posts
the results as a single comment on the PR itself. Three specialists run in
parallel, and later pushes edit that same comment in place rather than
piling up new ones.

<style>
.flow-diagram {
  --fd-stage-bg: #e8eefc;
  --fd-stage-border: #5b7cd6;
  --fd-stage-fg: #1a1a2e;
  --fd-specialist-bg: #eaf7ef;
  --fd-specialist-border: #3fa66d;
  --fd-specialist-fg: #0f2f1c;
  --fd-detail-fg: #4a4a58;
  --fd-arrow: #9aa5b1;
  margin: 2em 0 2.5em;
}
[data-md-color-scheme="slate"] .flow-diagram {
  --fd-stage-bg: #22314f;
  --fd-stage-border: #7b93e0;
  --fd-stage-fg: #e7ecff;
  --fd-specialist-bg: #1e3a2c;
  --fd-specialist-border: #5fce8f;
  --fd-specialist-fg: #dff5e8;
  --fd-detail-fg: #b7bccb;
  --fd-arrow: #6b7686;
}
.flow-diagram .fd-row {
  display: flex;
  align-items: stretch;
  justify-content: center;
  flex-wrap: wrap;
  gap: 0.75em;
}
.flow-diagram .fd-branches {
  gap: 1em;
}
.flow-diagram .fd-node {
  flex: 1 1 200px;
  max-width: 260px;
  border-radius: 12px;
  border: 1.5px solid var(--fd-stage-border);
  background: var(--fd-stage-bg);
  color: var(--fd-stage-fg);
  padding: 0.9em 1.1em;
  cursor: default;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
  outline: none;
}
.flow-diagram .fd-node.fd-specialist {
  border-color: var(--fd-specialist-border);
  background: var(--fd-specialist-bg);
  color: var(--fd-specialist-fg);
}
.flow-diagram .fd-node:hover,
.flow-diagram .fd-node:focus-visible {
  transform: translateY(-2px);
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.15);
}
.flow-diagram .fd-title {
  font-weight: 600;
  font-size: 0.95em;
}
.flow-diagram .fd-detail {
  color: var(--fd-detail-fg);
  font-size: 0.85em;
  line-height: 1.35;
  max-height: 0;
  opacity: 0;
  overflow: hidden;
  transition: max-height 0.2s ease, opacity 0.2s ease, margin-top 0.2s ease;
}
.flow-diagram .fd-node:hover .fd-detail,
.flow-diagram .fd-node:focus-visible .fd-detail {
  max-height: 6em;
  opacity: 1;
  margin-top: 0.5em;
}
.flow-diagram .fd-arrow {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--fd-arrow);
  font-size: 1.3em;
  flex: 0 0 auto;
}
.flow-diagram .fd-arrow-down {
  width: 100%;
  padding: 0.1em 0;
}
.flow-diagram .fd-split-label {
  text-align: center;
  font-size: 0.85em;
  color: var(--fd-detail-fg);
  margin-bottom: 0.6em;
}
@media (max-width: 600px) {
  .flow-diagram .fd-row { flex-direction: column; align-items: stretch; }
  .flow-diagram .fd-arrow:not(.fd-arrow-down) { transform: rotate(90deg); }
  .flow-diagram .fd-node { max-width: none; }
}
</style>

<div class="flow-diagram">
  <div class="fd-row">
    <div class="fd-node" tabindex="0">
      <div class="fd-title">Pull request opened or updated</div>
      <div class="fd-detail">You push code or open a PR — that's the only step you take.</div>
    </div>
    <div class="fd-arrow">&rarr;</div>
    <div class="fd-node" tabindex="0">
      <div class="fd-title">Webhook received</div>
      <div class="fd-detail">GitHub calls the bot automatically the moment your PR changes.</div>
    </div>
    <div class="fd-arrow">&rarr;</div>
    <div class="fd-node" tabindex="0">
      <div class="fd-title">Queued for review</div>
      <div class="fd-detail">The request is saved in line so nothing gets missed, even under heavy traffic.</div>
    </div>
  </div>

  <div class="fd-arrow fd-arrow-down">&darr;</div>

  <div class="fd-split-label">Three specialists check the code at once</div>
  <div class="fd-row fd-branches">
    <div class="fd-node fd-specialist" tabindex="0">
      <div class="fd-title">Security Review</div>
      <div class="fd-detail">Looks for risky code, like exposed secrets or unsafe input handling.</div>
    </div>
    <div class="fd-node fd-specialist" tabindex="0">
      <div class="fd-title">Performance Review</div>
      <div class="fd-detail">Flags code that could run slowly or waste resources.</div>
    </div>
    <div class="fd-node fd-specialist" tabindex="0">
      <div class="fd-title">Code Quality Review</div>
      <div class="fd-detail">Suggests cleaner, easier-to-maintain code.</div>
    </div>
  </div>

  <div class="fd-arrow fd-arrow-down">&darr;</div>

  <div class="fd-row">
    <div class="fd-node" tabindex="0">
      <div class="fd-title">Findings combined</div>
      <div class="fd-detail">All three reports are merged into one clear summary.</div>
    </div>
    <div class="fd-arrow">&rarr;</div>
    <div class="fd-node" tabindex="0">
      <div class="fd-title">Posted as a PR comment</div>
      <div class="fd-detail">The summary appears directly on your pull request — no dashboard required.</div>
    </div>
  </div>
</div>

*(Hover or tab to a step for a plain-language explanation.)*

## What a review looks like

A real posted comment, condensed to one finding per specialist:

```markdown
## 🤖 Automated Code Review — PR #42
_3 specialists · llama-3.3-70b-versatile (groq) · 4.2s · ~$0.0021_

### 🔒 Security — 1 finding
| Severity | Line | Issue | Suggested fix |
| --- | --- | --- | --- |
| 🔴 critical | `app/auth.py:88` | API key is logged in plaintext when the request fails | Log only the key's length/hash, never the raw value |

### ⚡ Performance — 1 finding
| Impact | Line | Issue | Suggestion |
| --- | --- | --- | --- |
| 🟡 medium | `app/api/users.py:145` | N+1 | Batch these lookups into a single query |

### 🧹 Code Quality — 1 finding
| Category | Line | Issue | Refactoring suggestion |
| --- | --- | --- | --- |
| duplication | `app/utils/format.py:22` | Date-formatting logic is duplicated across three modules | Extract a shared helper |

---
<sub>Runtime 4.2s · 1,842 tok in / 612 tok out · est. $0.0021 · provider: groq</sub>
```

If a specialist's own check fails outright, its section says so plainly
instead of vanishing — partial failure is always visible, never silent.

## Two tracks

- **Local** — run the engine on your own machine against a webhook-forwarding
  tool, for development and debugging.
- **Hosted** — deploy to Render with a real GitHub webhook, for a durable,
  always-on reviewer.

Both are covered by the same [setup guide](setup/index.md), which shares its
first four steps regardless of which track you pick.

## The one command to remember

```bash
uv run python -m scripts.doctor
```

Run it any time, from a fresh clone or mid-setup. It answers three
questions: where am I, what's missing, and what's next — without ever
mutating anything.

**[Get started →](setup/index.md)**
