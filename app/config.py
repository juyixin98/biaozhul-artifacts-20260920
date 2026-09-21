from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # e.g. postgresql+psycopg2://civic:civic@localhost:5432/civicledger
    database_url: str = "postgresql+psycopg2://civic:civic@localhost:5432/civicledger"


settings = Settings()
