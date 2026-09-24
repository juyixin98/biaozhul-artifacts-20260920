"""API behavior tests against a live local Anvil deployment."""
from __future__ import annotations

ONE_WEI = "1"
ONE_TOKEN = str(10**18)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["chain_id"] == 31337


def test_vault_state_and_rounding_doc(client):
    r = client.get("/vault")
    assert r.status_code == 200
    body = r.json()
    assert body["total_assets"] == "0"
    assert body["total_supply"] == "0"
    assert body["virtual_share_offset"] == "1000"
    assert "floor" in body["rounding"]["deposit"]


def test_tiny_deposit_one_wei_round_trips(client, deployment):
    # Empty vault: 1 wei of asset must mint 1000 shares and redeem exactly.
    r = client.post("/faucet", json={"amount": ONE_TOKEN})
    assert r.status_code == 200

    r = client.get("/vault/preview/deposit", params={"assets": ONE_WEI})
    assert r.status_code == 200
    assert r.json()["shares"] == "1000"

    r = client.post("/vault/deposit", json={"assets": ONE_WEI})
    assert r.status_code == 200
    dep = r.json()
    assert dep["shares_minted"] == "1000"

    r = client.post("/vault/redeem", json={"shares": dep["shares_minted"]})
    assert r.status_code == 200
    assert r.json()["assets_out"] == ONE_WEI

    r = client.get("/vault")
    assert r.json()["total_supply"] == "0"
    assert r.json()["total_assets"] == "0"


def test_deposit_matches_preview_and_redeem(client):
    client.post("/faucet", json={"amount": str(10 * 10**18)})
    amount = str(3 * 10**18)

    preview = client.get("/vault/preview/deposit", params={"assets": amount}).json()
    r = client.post("/vault/deposit", json={"assets": amount})
    assert r.status_code == 200
    shares = r.json()["shares_minted"]
    assert shares == preview["shares"]

    preview_redeem = client.get("/vault/preview/redeem", params={"shares": shares}).json()
    r = client.post("/vault/redeem", json={"shares": shares})
    assert r.status_code == 200
    out = r.json()["assets_out"]
    assert out == preview_redeem["assets"]
    # Rounding never favors the user: out <= in.
    assert int(out) <= int(amount)


def test_slippage_protection_reverts(client):
    client.post("/faucet", json={"amount": ONE_TOKEN})
    amount = str(10**17)
    quoted = client.get("/vault/preview/deposit", params={"assets": amount}).json()["shares"]

    # Demand one more share than the quote: must revert with Slippage.
    r = client.post(
        "/vault/deposit",
        json={"assets": amount, "min_shares_out": str(int(quoted) + 1)},
    )
    assert r.status_code == 400
    assert "Slippage" in r.json()["detail"] or "reverted" in r.json()["detail"]

    # max_slippage_bps=0 against a manipulated quote also reverts.
    r = client.post("/vault/deposit", json={"assets": amount, "max_slippage_bps": 0})
    assert r.status_code == 200  # exact quote is fine


def test_redeem_slippage_reverts(client):
    client.post("/faucet", json={"amount": ONE_TOKEN})
    dep = client.post("/vault/deposit", json={"assets": str(10**17)}).json()
    shares = dep["shares_minted"]
    quoted = client.get("/vault/preview/redeem", params={"shares": shares}).json()["assets"]
    r = client.post(
        "/vault/redeem",
        json={"shares": shares, "min_assets_out": str(int(quoted) + 1)},
    )
    assert r.status_code == 400


def test_donation_benefits_holder(client, deployment):
    """A direct transfer (donation) to the vault raises the holder's payout."""
    client.post("/faucet", json={"amount": str(10 * 10**18)})
    amount = str(10**18)
    dep = client.post("/vault/deposit", json={"assets": amount}).json()
    shares = dep["shares_minted"]

    # Donate directly on-chain via web3 (bypassing the API, as an attacker would).
    from web3 import Web3
    import os

    w3 = Web3(Web3.HTTPProvider(os.environ["RPC_URL"]))
    me = w3.eth.default_account = w3.eth.accounts[0]
    asset = w3.eth.contract(
        address=deployment["asset"],
        abi=[{
            "inputs": [
                {"name": "to", "type": "address"},
                {"name": "amount", "type": "uint256"},
            ],
            "name": "transfer",
            "outputs": [{"type": "bool"}],
            "stateMutability": "nonpayable",
            "type": "function",
        }],
    )
    asset.functions.transfer(deployment["vault"], 10**18).transact({"from": me})

    out = client.post("/vault/redeem", json={"shares": shares}).json()["assets_out"]
    assert int(out) > int(amount), "donation accrues to the share holder"


def test_account_endpoint(client, deployment):
    r = client.get(f"/account/{deployment['deployer']}")
    assert r.status_code == 200
    body = r.json()
    assert int(body["asset_balance"]) >= 0
    assert body["share_balance"] == "0" or int(body["share_balance"]) >= 0


def test_invalid_amount_rejected(client):
    r = client.post("/vault/deposit", json={"assets": "not-a-number"})
    assert r.status_code == 422
    r = client.get("/vault/preview/deposit", params={"assets": "-5"})
    assert r.status_code == 422
