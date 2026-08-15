from datetime import time

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Env-var names that hold plain operational config, not credentials. LISTED =
# OPERATIONAL (lives in .env.config, freely editable by anyone including an
# agent); EVERYTHING ELSE IS SECRET BY DEFAULT (lives in .env, which an agent
# must never open -- see CLAUDE.md's "Secret handling" section).
#
# Every entry is a LITERAL key name, enumerated one by one -- never a prefix or
# glob. A pattern would silently classify future keys that happen to match,
# which is exactly the secret-by-default guarantee this list exists to provide.
#
# Adding a setting here is a deliberate classification decision, not a
# formality: tests/test_config.py fails if a listed key is found in .env or an
# unlisted key is found in .env.config.
OPERATIONAL_KEYS = frozenset(
    {
        "LLM_PROVIDER",
        "LLM_MODEL",
        "GROQ_MODEL",
        "KEY_USAGE_TOKEN_CAP",
        "KEY_USAGE_COST_CAP_USD",
        "KEY_USAGE_RESET_TIME_UTC",
        "GCP_PROJECT",
        "GCP_LOCATION",
        "LLM_REQUEST_TIMEOUT_SECONDS",
        "DISPATCHER_IDLE_SLEEP_SECONDS",
        "DEFAULT_RETRY_AFTER_SECONDS",
        "DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS",
        "DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS",
        "DISPATCHER_MAX_FAILURE_ATTEMPTS",
        "DISPATCHER_MAX_NOTICE_POST_ATTEMPTS",
        "DISPATCHER_MIN_RETRY_AFTER_SECONDS",
        "DISPATCHER_BACKOFF_JITTER_SECONDS",
        "DISPATCHER_REREVIEW_COOLDOWN_SECONDS",
        "DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS",
        "DISPATCHER_REREVIEW_COOLDOWN_FACTOR",
        "DISPATCHER_NOTICE_SWEEP_BATCH_SIZE",
        "RENDER_SERVICE_NAME",
        "GITHUB_TARGET_REPO",
        "PUBLIC_BASE_URL",
    }
)


class Settings(BaseSettings):
    # Two files, one Settings. .env holds credentials and identity; .env.config
    # holds operational settings (OPERATIONAL_KEYS above). The LAST file wins on
    # a key present in both, so .env.config -- the designated home -- outranks a
    # stale line left in .env. A real process env var still beats both, which is
    # why Render is unaffected: neither file exists in the container.
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.config"), extra="ignore"
    )

    github_app_id: int = 0
    github_app_installation_id: int = 0
    github_app_private_key_path: str = "./github-app-private-key.pem"
    github_app_private_key_b64: str = ""
    github_webhook_secret: str = ""
    github_target_repo: str = ""
    public_base_url: str = ""  # set from RENDER_EXTERNAL_URL on Render; PUBLIC_BASE_URL override

    llm_provider: str = "gemini"
    # ``llm_model`` is consumed by the gemini (google-genai) provider only.
    # Groq is a different model family (Llama, via a different vendor), so it
    # gets its own var — a single shared LLM_MODEL became ambiguous the moment
    # a second provider family entered the picture (see CLAUDE.md task 8 / PR
    # report for the reasoning).
    llm_model: str = "gemini-flash-latest"

    gemini_api_key: str = ""
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # --- Vertex AI (LLM_PROVIDER=vertex). Unlike gemini/groq, the credential
    # is a GCP service-account identity rather than an API-key string:
    # GCP_SERVICE_ACCOUNT_KEY_B64 (hosted) -> a local key file -> implicit ADC.
    # See app/providers/vertex_credentials.py for the resolution order.
    # An OPTIONAL override: unset means "use the project_id embedded in the
    # resolved service-account key", so an operator handed nothing but a JSON
    # key needs no separate project lookup.
    gcp_project: str = ""
    # Which Vertex regional endpoint to call -- not an account property, so the
    # default needs no lookup either.
    gcp_location: str = "us-central1"
    gcp_service_account_key_b64: str = ""
    gcp_service_account_key_path: str = "./gcp-service-account-key.json"
    # Ceiling on a single LLM request, in seconds. The dispatcher is a single
    # serial consumer of the whole queue (app/queue/dispatcher.py) -- a hung
    # call with no timeout would stall every pending PR's review, not just
    # one, for however long the SDK's own default timeout is (several
    # minutes). 45s is well under that while still tolerating a genuinely
    # slow-but-healthy response.
    llm_request_timeout_seconds: float = 45.0

    database_url: str = ""
    dispatcher_idle_sleep_seconds: float = 1.0
    default_retry_after_seconds: float = 60.0
    dispatcher_failure_base_backoff_seconds: float = 2.0
    dispatcher_failure_max_backoff_seconds: float = 300.0
    dispatcher_max_failure_attempts: int = 5
    dispatcher_max_notice_post_attempts: int = 3
    dispatcher_min_retry_after_seconds: float = 1.0
    dispatcher_backoff_jitter_seconds: float = 0.0
    dispatcher_rereview_cooldown_seconds: float = 300.0
    dispatcher_rereview_cooldown_max_seconds: float = 3600.0
    # ge=1.0: a factor < 1 would shrink the cooldown across escalation
    # levels instead of lengthening it, defeating the point of escalation.
    dispatcher_rereview_cooldown_factor: float = Field(default=2.0, ge=1.0)
    # gt=0: 0 would silently disable the notice sweep entirely, and -1 means
    # "no limit" in SQLite, silently reverting to the unbounded pre-fix
    # behavior this setting exists to prevent.
    dispatcher_notice_sweep_batch_size: int = Field(default=20, gt=0)

    # --- Proactive per-key daily usage cap. Both caps default to None
    # (feature off): a deployment that sets neither env var behaves exactly
    # as before. KEY_USAGE_TOKEN_CAP WINS OUTRIGHT when both are set -- the
    # cost cap is then not consulted at all, not used as a tiebreak. The
    # reset time is a plain "HH:MM" (or "HH:MM:SS") UTC wall-clock string;
    # a `time` field makes pydantic parse it, giving arbitrary granularity
    # rather than whole-hour-only resets, specifically so a demo can set the
    # reset a couple of minutes out instead of waiting for the next hour.
    # gt=0: a 0 or negative cap would make the dispatcher's `tokens >= 0` /
    # `cost >= 0` comparison unconditionally true, deferring every review
    # forever -- and that deferral is STICKY, since a ticket's not_before is
    # already set to a real future timestamp by the time it happens, so
    # fixing the env var and redeploying does not release already-deferred
    # tickets.
    key_usage_token_cap: int | None = Field(default=None, gt=0)
    key_usage_cost_cap_usd: float | None = Field(default=None, gt=0)
    key_usage_reset_time_utc: time = Field(default=time(4, 0))

    # --- Optional operator tooling: read only by scripts/deploy.py on the
    # operator's own machine. Never set on the deployed service, never added
    # to render.yaml. Absence degrades a check to SKIPPED, never to an error.
    uptimerobot_api_key: str = ""
    render_api_key: str = ""
    render_service_name: str = "pr-review-engine"


settings = Settings()
