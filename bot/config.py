from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    bot_token: str
    admin_id: int | None = None
    keep_last: int = 1500
    logging_enabled: bool = True
    db_path: str = "data/markov.db"

    model_config = {"env_file": ".env"}


settings = Settings()
