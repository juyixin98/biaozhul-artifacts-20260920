"""端到端测试：Python 生成叶子/证明 → 真实 Anvil 上的 Solidity 合约校验。

覆盖验收点：
1. Python 预测的 CREATE 地址与实际部署地址一致（根才能正确绑定合约地址）；
2. 跨合约证明重放失败（A 的证明在 B 上无效）；
3. 重复索引：单笔二次领取失败、批次内重复失败；
4. 批次中单项无效 → 整批失败，无部分已领状态；
5. 转账失败 → 整批回滚；
6. Python 生成的证明在链上 verify 通过（双语言一致性）。
"""

from __future__ import annotations

import pytest
from eth_account import Account
from web3.exceptions import ContractLogicError

from app import chain as chain_mod
from app.merkle import get_proof

from conftest import KEY0, KEY1


# ---------- Python/Solidity 一致性与部署 ----------

def test_predicted_address_matches_deployment(w3, deployed, settings, allocations):
    # 独立验证 CREATE 地址预测：取当前 nonce → 预测 → 部署 → 比对实际地址。
    deployer = Account.from_key(KEY0).address
    nonce = w3.eth.get_transaction_count(deployer)
    predicted = chain_mod.predict_create_address(deployer, nonce)

    total = sum(a.amount_wei for a in allocations)
    contract, address, _root, used_nonce = chain_mod.deploy_claim_contract(
        w3, KEY0, allocations, settings.artifact_path, total
    )
    assert used_nonce == nonce
    assert address == predicted
    assert contract.address == predicted


def test_python_proofs_pass_onchain(w3, deployed, settings):
    contract, allocations, _total = deployed
    chain_id = w3.eth.chain_id
    leaves = chain_mod.leaves_for(allocations, chain_id, contract.address)

    # 链上登记的根必须等于 Python 算出的根
    from app.merkle import get_root

    onchain_root = contract.functions.merkleRoot().call()
    # web6 的 HexBytes.hex() 不带 0x
    assert get_root(leaves).hex() == onchain_root.hex().removeprefix("0x")

    # 领取 index 0，证明完全由 Python 生成
    a = allocations[0]
    proof = get_proof(leaves, a.index)
    receipt = chain_mod.send_claim(w3, contract, KEY1, a.index, a.account, a.amount_wei, proof)
    assert receipt["status"] == 1
    assert contract.functions.isClaimed(0).call() is True


def test_contract_funded_correctly(w3, deployed):
    contract, _allocations, total = deployed
    assert w3.eth.get_balance(contract.address) == total


# ---------- 重复索引 ----------

def test_duplicate_single_claim_reverts(w3, deployed):
    contract, allocations, _ = deployed
    leaves = chain_mod.leaves_for(allocations, w3.eth.chain_id, contract.address)
    a = allocations[1]
    proof = get_proof(leaves, a.index)

    chain_mod.send_claim(w3, contract, KEY0, a.index, a.account, a.amount_wei, proof)
    # send_claim 直接发送交易；重复索引使交易链上回滚（status=0）
    with pytest.raises(chain_mod.TransactionReverted):
        chain_mod.send_claim(w3, contract, KEY0, a.index, a.account, a.amount_wei, proof)


def test_duplicate_index_inside_batch_reverts_no_partial_state(w3, deployed):
    contract, allocations, _ = deployed
    leaves = chain_mod.leaves_for(allocations, w3.eth.chain_id, contract.address)
    a0 = allocations[0]
    proof0 = get_proof(leaves, 0)

    before = {a.account: w3.eth.get_balance(a.account) for a in allocations}

    with pytest.raises((ContractLogicError, chain_mod.BatchRejected)):
        chain_mod.send_claim_batch(
            w3,
            contract,
            KEY0,
            [0, 0],
            [a0.account, a0.account],
            [a0.amount_wei, a0.amount_wei],
            [proof0, proof0],
        )

    # 原子性：没有任何索引被标记、没有任何 ETH 移动
    assert contract.functions.isClaimed(0).call() is False
    assert w3.eth.get_balance(a0.account) == before[a0.account]


# ---------- 批次单项无效 ----------

def test_batch_one_invalid_proof_reverts_entire_batch(w3, deployed):
    contract, allocations, _ = deployed
    leaves = chain_mod.leaves_for(allocations, w3.eth.chain_id, contract.address)
    a0, a2 = allocations[0], allocations[2]
    proof0 = get_proof(leaves, 0)
    proof2 = get_proof(leaves, 2)

    bal0_before = w3.eth.get_balance(a0.account)
    bal2_before = w3.eth.get_balance(a2.account)

    # 第二项：数量 +1，叶子不匹配根（证明仍拿原证明）。
    # estimateGas 阶段节点返回 InvalidProof 错误选择器（0xf25ea2d5），批次不会发出。
    with pytest.raises((ContractLogicError, chain_mod.BatchRejected)):
        chain_mod.send_claim_batch(
            w3,
            contract,
            KEY0,
            [0, 2],
            [a0.account, a2.account],
            [a0.amount_wei, a2.amount_wei + 1],
            [proof0, proof2],
        )

    assert contract.functions.isClaimed(0).call() is False
    assert contract.functions.isClaimed(2).call() is False
    assert w3.eth.get_balance(a0.account) == bal0_before
    assert w3.eth.get_balance(a2.account) == bal2_before


def test_batch_after_prior_claim_reverts_and_keeps_others_unclaimed(w3, deployed):
    contract, allocations, _ = deployed
    leaves = chain_mod.leaves_for(allocations, w3.eth.chain_id, contract.address)
    a0, a1 = allocations[0], allocations[1]
    chain_mod.send_claim(w3, contract, KEY0, 0, a0.account, a0.amount_wei, get_proof(leaves, 0))

    bal1_before = w3.eth.get_balance(a1.account)
    with pytest.raises((ContractLogicError, chain_mod.BatchRejected)):
        chain_mod.send_claim_batch(
            w3,
            contract,
            KEY0,
            [0, 1],
            [a0.account, a1.account],
            [a0.amount_wei, a1.amount_wei],
            [get_proof(leaves, 0), get_proof(leaves, 1)],
        )
    # 批次回滚不影响 index 0 的既成领取，index 1 也不能被"带上"
    assert contract.functions.isClaimed(0).call() is True
    assert contract.functions.isClaimed(1).call() is False
    assert w3.eth.get_balance(a1.account) == bal1_before


# ---------- 转账失败回滚 ----------

REJECTER_SOURCE = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
contract RejectReceiver {
    receive() external payable { revert("no eth"); }
}
"""


def test_batch_transfer_failure_reverts_entire_batch(w3, deployed):
    import json
    import os

    contract, allocations, _ = deployed

    # 使用主工程 forge build 产出的 RejectReceiver（拒绝接收 ETH）
    project_root = os.path.dirname(os.path.dirname(__file__))
    art = json.loads(
        open(os.path.join(project_root, "out", "RejectReceiver.sol", "RejectReceiver.json")).read()
    )
    rejecter = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    acct = Account.from_key(KEY0)
    tx = rejecter.constructor().build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": w3.eth.chain_id,
            "gas": 200_000,
        }
    )
    signed = w3.eth.account.sign_transaction(tx, KEY0)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    rcpt = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(raw))
    rejecter_addr = rcpt["contractAddress"]

    # 部署一份把 index 0 受益人替换为 rejecter 的新合约
    from app.allocations import Allocation

    artifact_path = os.path.join(project_root, "out", "MerkleClaim.sol", "MerkleClaim.json")
    modified = [
        Allocation(
            index=a.index,
            account=(rejecter_addr if a.index == 0 else a.account),
            amount_wei=a.amount_wei,
        )
        for a in allocations
    ]
    total = sum(a.amount_wei for a in modified)
    contract2, _, _, _ = chain_mod.deploy_claim_contract(w3, KEY0, modified, artifact_path, total)

    leaves = chain_mod.leaves_for(modified, w3.eth.chain_id, contract2.address)
    a0m, a1m = modified[0], modified[1]
    bal1_before = w3.eth.get_balance(a1m.account)

    with pytest.raises((ContractLogicError, chain_mod.BatchRejected)):
        chain_mod.send_claim_batch(
            w3,
            contract2,
            KEY0,
            [0, 1],
            [a0m.account, a1m.account],
            [a0m.amount_wei, a1m.amount_wei],
            [get_proof(leaves, 0), get_proof(leaves, 1)],
        )
    assert contract2.functions.isClaimed(0).call() is False
    assert contract2.functions.isClaimed(1).call() is False
    assert w3.eth.get_balance(a1m.account) == bal1_before
    assert w3.eth.get_balance(contract2.address) == total


# ---------- 跨合约重放 ----------

def test_proof_from_contract_A_rejected_by_contract_B(w3, deployed, settings):
    contract_a, allocations, total = deployed

    # B 是一份全新部署的合约（相同分配、相同根，但地址不同）。
    # 叶子绑定合约地址：A 的叶子 != B 的叶子，所以 A 的证明在 B 上必失败。
    contract_b, _, _, _ = chain_mod.deploy_claim_contract(
        w3,
        settings.deployer_private_key,
        allocations,
        settings.artifact_path,
        total,
    )
    assert contract_a.address != contract_b.address

    leaves_a = chain_mod.leaves_for(allocations, w3.eth.chain_id, contract_a.address)
    a = allocations[0]
    proof_a = get_proof(leaves_a, 0)

    bal_before = w3.eth.get_balance(a.account)
    # 正常：A 接受自己的证明
    chain_mod.send_claim(w3, contract_a, KEY0, a.index, a.account, a.amount_wei, proof_a)
    assert w3.eth.get_balance(a.account) == bal_before + a.amount_wei  # 从 A 领到一次

    # 重放：把 A 的 (index, account, amount, proof) 原封不动发给 B
    with pytest.raises(chain_mod.TransactionReverted):
        chain_mod.send_claim(w3, contract_b, KEY0, a.index, a.account, a.amount_wei, proof_a)
    assert contract_b.functions.isClaimed(0).call() is False
    # B 不能再给该账户发一份
    assert w3.eth.get_balance(a.account) == bal_before + a.amount_wei


def test_proof_with_tampered_account_rejected(w3, deployed):
    contract, allocations, _ = deployed
    leaves = chain_mod.leaves_for(allocations, w3.eth.chain_id, contract.address)
    a0, a1 = allocations[0], allocations[1]

    before = w3.eth.get_balance(a1.account)
    # index 0 的证明，但把收款账户改成 index 1 的人
    with pytest.raises(chain_mod.TransactionReverted):
        chain_mod.send_claim(w3, contract, KEY0, 0, a1.account, a0.amount_wei, get_proof(leaves, 0))
    assert contract.functions.isClaimed(0).call() is False
    # anvil 给测试账户预充值，只能断言没有因领取而增加
    assert w3.eth.get_balance(a1.account) == before
