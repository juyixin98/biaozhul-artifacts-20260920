import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://consentvault:consentvault@localhost:55435/consentvault",
)
