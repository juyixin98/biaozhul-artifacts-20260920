"""Smoke test: Anvil manual mining, deploy, deposit, event fetch."""
from app import onchain
from tests.conftest import DEPLOY_KEY


def test_smoke_deploy_deposit(chain):
    w3 = chain
    sender = onchain.TxSender(w3)
    addr = onchain.deploy_vault(w3, sender, DEPLOY_KEY)
    vault = onchain.vault_at(w3, addr)
    deploy_block = w3.eth.block_number

    tx = sender.vault_call(DEPLOY_KEY, vault, "deposit", value=1234)
    onchain.mine_blocks(w3, 1)
    rcpt = w3.eth.wait_for_transaction_receipt(tx)
    assert rcpt["status"] == 1

    logs = onchain.vault_logs(w3, addr, deploy_block + 1, deploy_block + 1)
    assert len(logs) == 1
    assert logs[0]["transactionHash"] == tx
    assert logs[0]["logIndex"] == 0
