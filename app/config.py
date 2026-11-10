from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # http(s)://... -> Streamable HTTP; anything else -> stdio command line.
    mcp_signal_server: str = "python -m mock_mcp.server"
    mcp_signal_token: str = ""
    # Server with find_customer/create_ticket/handoff_to_human. Empty = signal server.
    mcp_customer_server: str = ""
    mcp_call_timeout: float = 15.0       # seconds per tool call before giving up

    # Claude
    anthropic_api_key: str = ""          # empty -> SDK default credential chain
    bot_model: str = "claude-opus-5-5"
    bot_summary_model: str = ""          # empty -> bot_model
    bot_effort: str = "medium"           # low | medium | high | xhigh | max
    bot_max_tool_calls: int = 5
    bot_history_window: int = 10
    bot_timezone: str = "Europe/Madrid"
    bot_fallbacks: bool = True           # server-side refusal fallback ("default" routing)

    app_db_path: str = "data/app.db"
    # Return tool calls + token usage to the chat page (debug panel). Off for real customers.
    chat_debug: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
