from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Overridden by the DATABASE_URL environment variable.
    database_url: str = "postgresql+psycopg://civic:civic@localhost:5432/civicledger"


settings = Settings()
