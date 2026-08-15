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
    # vertex's credential is a base64-encoded service-account JSON key, not an
    # API-key string -- but it is resolved through the same numbered-slot
    # mechanism (credentials.resolve), so it belongs in the same table.
    # app/providers/vertex_credentials.py layers the local-file and
    # implicit-ADC fallbacks on top of what this entry resolves.
    #
    # VERTEX_MODEL, not LLM_MODEL: vertex and gemini are the same SDK but
    # different model catalogs -- gemini-flash-latest does not exist as a
    # Vertex publisher model (404). Sharing one var made a DB provider flip
    # between them guaranteed-broken. Completes the split whose reasoning
    # app/config.py already records for GROQ_MODEL.
    "vertex": ("GCP_SERVICE_ACCOUNT_KEY_B64", "VERTEX_MODEL"),
}

# provider -> the runtime_config column holding its active API-key-slot
# index override. A hardcoded whitelist, not a naming convention derived at
# call time -- every SQL statement that touches one of these columns looks
# the name up through this dict rather than building it from a caller's
# `provider` string, so this dict IS the injection guard for those callers.
KEY_INDEX_COLUMNS = {
    "gemini": "gemini_key_index",
    "groq": "groq_key_index",
    "vertex": "vertex_key_index",
}

# provider -> the runtime_config column holding its model override. Same
# hardcoded-whitelist role as KEY_INDEX_COLUMNS above: psycopg parameterizes
# values but not column identifiers, so looking the name up here -- rather
# than building it from a caller's `provider` string -- IS the injection
# guard for every statement that touches one of these columns.
MODEL_COLUMNS = {
    "gemini": "gemini_model",
    "groq": "groq_model",
    "vertex": "vertex_model",
}
