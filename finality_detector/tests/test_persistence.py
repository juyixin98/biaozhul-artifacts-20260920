"""Persistence: a restart over the same SQLite file rebuilds the same view."""

from fastapi.testclient import TestClient

from app import crypto
from app.main import create_app

CHAIN_ID = "test-chain"


def test_restart_rebuilds_identical_conclusions(tmp_path):
    db = str(tmp_path / "persist.db")
    weights = {"alice": 40, "bob": 40, "carol": 20}
    keys = {vid: crypto.generate_private_key() for vid in weights}

    def make_client():
        return TestClient(create_app(db_path=db, chain_id=CHAIN_ID))

    # --- phase 1: run the chain, finalize epoch 0, freeze epoch 1 -----------
    with make_client() as client:
        validators = [
            {
                "validator_id": vid,
                "weight": weights[vid],
                "public_key": crypto.encode_public_key(sk.public_key()),
            }
            for vid, sk in keys.items()
        ]
        assert client.post(
            "/epochs", json={"epoch_id": 0, "height": 10, "validators": validators}
        ).status_code == 201
        assert client.post(
            "/epochs", json={"epoch_id": 1, "height": 20, "validators": validators}
        ).status_code == 201

        def vote(epoch, height, vid, value):
            return client.post(
                "/votes",
                json={
                    "epoch_id": epoch,
                    "height": height,
                    "validator_id": vid,
                    "value": value,
                    "signature": crypto.sign_vote(keys[vid], CHAIN_ID, epoch, height, value),
                },
            )

        # epoch 0: finalize A; bob equivocates and is excluded afterwards.
        vote(0, 10, "alice", "A")
        vote(0, 10, "bob", "A")
        vote(0, 10, "carol", "A")
        vote(0, 10, "bob", "B")  # equivocation evidence stored

        # epoch 1: finalize C, then a conflicting certificate freezes it.
        vote(1, 20, "alice", "C")
        vote(1, 20, "bob", "C")
        cert = {
            "epoch_id": 1,
            "height": 20,
            "value": "D",
            "signatures": [
                {"validator_id": vid, "signature": crypto.sign_vote(keys[vid], CHAIN_ID, 1, 20, "D")}
                for vid in ("alice", "bob")
            ],
        }
        assert client.post("/certificates", json=cert).json()["status"] == "finality_conflict"

        before_e0 = client.get("/epochs/0").json()
        before_e1 = client.get("/epochs/1").json()
        before_alarms = client.get("/alarms").json()
        before_evidence = client.get("/epochs/0/evidence").json()

    # --- phase 2: "restart" — brand new app over the same database ----------
    with make_client() as client:
        assert client.get("/epochs/0").json() == before_e0
        assert client.get("/epochs/1").json() == before_e1
        assert client.get("/alarms").json() == before_alarms
        assert client.get("/epochs/0/evidence").json() == before_evidence

        # Rebuilt conclusions are internally consistent.
        rebuild = client.get("/rebuild").json()
        by_epoch = {e["epoch_id"]: e for e in rebuild["epochs"]}
        assert by_epoch[0]["finalized_value"] == "A"
        assert by_epoch[0]["excluded"] == ["bob"]
        assert by_epoch[1]["finalized_value"] == "C"
        assert by_epoch[1]["frozen"] is True
        assert all(e["consistent"] for e in rebuild["epochs"])

        # The frozen epoch still refuses progress after the restart.
        resp = client.post(
            "/epochs", json={"epoch_id": 2, "height": 30, "validators": validators}
        )
        assert resp.status_code == 409
        assert resp.json()["error"] == "predecessor_frozen"
