from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_app_id: int = 0
    github_app_installation_id: int = 0
    github_app_private_key_path: str = "./github-app-private-key.pem"
    github_app_private_key_b64: str = ""
    github_webhook_secret: str = ""
    github_target_repo: str = ""
    public_base_url: str = ""  # set from RENDER_EXTERNAL_URL on Render; PUBLIC_BASE_URL override

    llm_provider: str = "gemini"
    # ``llm_model`` is consumed by the google-genai family (vertex/gemini) only.
    # Groq is a different model family (Llama, via a different vendor), so it
    # gets its own var — a single shared LLM_MODEL became ambiguous the moment
    # a second provider family entered the picture (see CLAUDE.md task 8 / PR
    # report for the reasoning).
    llm_model: str = "gemini-flash-latest"

    google_cloud_project: str = ""
    google_cloud_location: str = "us-central1"
    gemini_api_key: str = ""
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    github_models_token: str = ""
    github_models_model: str = "openai/gpt-4o-mini"

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
    # gt=0: 0 would silently disable the notice sweep entirely, and -1 means
    # "no limit" in SQLite, silently reverting to the unbounded pre-fix
    # behavior this setting exists to prevent.
    dispatcher_notice_sweep_batch_size: int = Field(default=20, gt=0)


settings = Settings()
