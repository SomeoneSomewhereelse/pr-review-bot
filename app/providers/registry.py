"""Single source of truth for provider -> env-var-name mappings.

Both app/ (runtime credential resolution: credentials.py, factory.py) and
scripts/ (deploy verification, the set_provider/set_api_key CLIs) read this.
Previously duplicated as scripts/deploy.py's private _PROVIDERS dict; moved
here because app/ now needs the same mapping and must not import from
scripts/ (the dependency direction runs the other way everywhere else in
this codebase).
"""

from __future__ import annotations

# provider -> (credential env var, model env var)
PROVIDERS = {
    "gemini": ("GEMINI_API_KEY", "LLM_MODEL"),
    "groq": ("GROQ_API_KEY", "GROQ_MODEL"),
    "github_models": ("GITHUB_MODELS_TOKEN", "GITHUB_MODELS_MODEL"),
}

# provider -> the runtime_config column holding its active API-key-slot
# index override. A hardcoded whitelist, not a naming convention derived at
# call time -- every SQL statement that touches one of these columns looks
# the name up through this dict rather than building it from a caller's
# `provider` string, so this dict IS the injection guard for those callers.
KEY_INDEX_COLUMNS = {
    "gemini": "gemini_key_index",
    "groq": "groq_key_index",
    "github_models": "github_models_key_index",
}
