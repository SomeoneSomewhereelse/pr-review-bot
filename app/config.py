from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_app_id: int = 0
    github_app_installation_id: int = 0
    github_app_private_key_path: str = "./github-app-private-key.pem"
    github_webhook_secret: str = ""

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

    queue_db_path: str = "./queue.db"
    dispatcher_idle_sleep_seconds: float = 1.0
    default_retry_after_seconds: float = 60.0


settings = Settings()
