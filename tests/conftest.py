import pytest

from app.config import Budget, Settings


@pytest.fixture()
def budget() -> Budget:
    return Budget(
        max_envs_per_label=64,
        max_propagation_steps=10_000,
        max_combinations_per_rule=4_096,
    )


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(db_path=str(tmp_path / "test.db"), log_symbol_names=True)
