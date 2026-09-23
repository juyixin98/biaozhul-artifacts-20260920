"""Shared fixtures: real Ed25519 keys, a fresh SQLite file per test."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import crypto  # noqa: E402
from app.main import create_app  # noqa: E402

CHAIN_ID = "test-chain"


class Harness:
    """Convenience wrapper: holds keys and signs votes like a real validator."""

    def __init__(self, client: TestClient, weights: dict[str, int]):
        self.client = client
        self.keys = {vid: crypto.generate_private_key() for vid in weights}
        self.weights = weights

    def create_epoch(self, epoch_id: int, height: int):
        validators = [
            {
                "validator_id": vid,
                "weight": self.weights[vid],
                "public_key": crypto.encode_public_key(sk.public_key()),
            }
            for vid, sk in self.keys.items()
        ]
        return self.client.post(
            "/epochs", json={"epoch_id": epoch_id, "height": height, "validators": validators}
        )

    def vote_body(self, epoch: int, height: int, vid: str, value: str) -> dict:
        return {
            "epoch_id": epoch,
            "height": height,
            "validator_id": vid,
            "value": value,
            "signature": crypto.sign_vote(self.keys[vid], CHAIN_ID, epoch, height, value),
        }

    def vote(self, epoch: int, height: int, vid: str, value: str):
        return self.client.post("/votes", json=self.vote_body(epoch, height, vid, value))

    def status(self, epoch_id: int):
        resp = self.client.get(f"/epochs/{epoch_id}")
        assert resp.status_code == 200
        return resp.json()


@pytest.fixture()
def harness(tmp_path):
    db = tmp_path / "test.db"
    app = create_app(db_path=str(db), chain_id=CHAIN_ID)
    with TestClient(app) as client:
        def _make(weights: dict[str, int]) -> Harness:
            return Harness(client, weights)

        yield _make
