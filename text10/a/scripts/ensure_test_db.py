"""Create the test database if it does not exist (used by docker compose test)."""

import os

import psycopg

BASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://civic:civic@localhost:5432/civicledger"
)
TEST_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://civic:civic@localhost:5432/civicledger_test"
)


def to_psycopg(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def db_name(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def main() -> None:
    name = db_name(TEST_URL)
    with psycopg.connect(to_psycopg(BASE_URL), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{name}"')
            print(f"created database {name}")
        else:
            print(f"database {name} already exists")


if __name__ == "__main__":
    main()
