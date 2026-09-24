"""端到端测试：HTTP API -> web3.py -> 本地 Anvil 上的 MerkleClaim。

覆盖验收点：
  - 证明生成、正常批量领取、链上状态变化
  - 跨交易重复索引拒绝
  - 批次内重复索引（直接构造链上调用）原子回滚
  - 批次中单项无效（篡改金额）原子回滚，无部分已领/无资金流出
  - 跨合约重放：第二个合约实例拒绝第一个实例的证明
  - 跨链重放：另一条 chainId 的 Anvil 上证明失效
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from web3 import Web3

from backend.chain import load_abi
from backend.merkle import build_tree_from_allocations, encode_leaf

REPO_ROOT = Path(__file__).resolve().parent.parent

ANVIL_KEY0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ZERO = "0x" + "00" * 20

# 与 examples/allocations.json 一致
ALLOCATIONS = json.loads((REPO_ROOT / "examples" / "allocations.json").read_text())["allocations"]
TOTAL_ALLOC = sum(int(a["amount"]) for a in ALLOCATIONS)


def _contract(w3, address):
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=load_abi())


# --------------------------------------------------------------------------
# API 层
# --------------------------------------------------------------------------

def test_health_and_status(http):
    h = http.get("/health").json()
    assert h["ok"] is True
    assert h["chain_id"] == 31337

    s = http.get("/status").json()
    assert s["chain_id"] == 31337
    assert s["token"] == ZERO
    assert s["allocation_count"] == len(ALLOCATIONS)


def test_root_matches_chain(deployment, http):
    s = http.get("/status").json()
    assert s["root"] == deployment["root"]


def test_proof_endpoint_and_verify(http):
    r = http.get("/proof/0")
    assert r.status_code == 200
    item = r.json()
    assert item["index"] == 0
    assert len(item["proof"]) == 3  # 8 个叶子 → 深度 3
    assert item["leaf"].startswith("0x")

    v = http.get("/verify/0").json()
    assert v["leaf_matches"] is True
    assert v["claimed"] is False

    assert http.get("/proof/999").status_code == 404


def test_batch_submit_success_then_duplicate_rejected(deployment, http, w3):
    addr = deployment["address"]
    c = _contract(w3, addr)
    acct0 = Web3.to_checksum_address(ALLOCATIONS[0]["account"])
    acct2 = Web3.to_checksum_address(ALLOCATIONS[2]["account"])

    # 领取索引 0,1
    bal0_before = w3.eth.get_balance(acct0)
    bal2_before = w3.eth.get_balance(acct2)
    r = http.post("/batch/submit", json={"indices": [0, 1]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == 1
    assert body["gas_used"] > 0

    assert c.functions.isClaimed(0).call() is True
    assert c.functions.isClaimed(1).call() is True
    assert w3.eth.get_balance(acct0) - bal0_before == int(ALLOCATIONS[0]["amount"])

    # 跨交易重复：再次领取含已领索引 1 的批次 → 链上回滚，API 返回 400
    r2 = http.post("/batch/submit", json={"indices": [1, 2]})
    assert r2.status_code == 400
    # 原子性：索引 2 没有被部分领取，账户 2 余额不变
    assert c.functions.isClaimed(2).call() is False
    assert w3.eth.get_balance(acct2) == bal2_before

    # 同批次索引重复：后端在出证明阶段直接拒绝
    r3 = http.post("/batch/prepare", json={"indices": [3, 3]})
    assert r3.status_code == 400


def test_prepare_does_not_mutate_state(deployment, http, w3):
    c = _contract(w3, deployment["address"])
    r = http.post("/batch/prepare", json={"indices": [4, 5]})
    assert r.status_code == 200
    body = r.json()
    assert body["function"] == "claimBatch"
    assert int(body["value"]) == 0  # 合约预存资金，claim 交易不携带 value
    assert len(body["items"]) == 2
    # prepare 不上链
    assert c.functions.isClaimed(4).call() is False
    assert c.functions.isClaimed(5).call() is False


# --------------------------------------------------------------------------
# 链上层：原子性、重放保护（web3.py 直接构造）
# --------------------------------------------------------------------------

def _claim_batch_raw(w3, addr, sender_key, items, tamper_amount_index: int | None = None,
                     tamper_account_index: int | None = None):
    """直接用后端证明组装 claimBatch 交易，可故意篡改某一项。"""
    c = _contract(w3, addr)
    chain_id = w3.eth.chain_id
    tree, norm = build_tree_from_allocations(
        [{"index": int(a["index"]), "account": a["account"], "amount": int(a["amount"])}
         for a in ALLOCATIONS], chain_id, addr)
    acct = w3.eth.account.from_key(sender_key)

    indices, accounts, amounts, proofs = [], [], [], []
    for pos, it in enumerate(items):
        idx = it["index"]
        a = norm[[n["index"] for n in norm].index(idx)]
        leaf = encode_leaf(chain_id, addr, a["index"], a["account"], a["amount"])
        p = tree.proof(leaf)
        amt = a["amount"]
        acct_addr = a["account"]
        if tamper_amount_index is not None and pos == tamper_amount_index:
            amt += 1
        if tamper_account_index is not None and pos == tamper_account_index:
            acct_addr = ALLOCATIONS[6]["account"]
        indices.append(a["index"])
        accounts.append(Web3.to_checksum_address(acct_addr))
        amounts.append(amt)
        proofs.append(list(p))

    fn = c.functions.claimBatch(indices, accounts, amounts, proofs)
    tx = fn.build_transaction({
        "from": acct.address,
        "value": 0,
        "gas": 3_000_000,
        "chainId": chain_id,
        "nonce": w3.eth.get_transaction_count(acct.address),
    })
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(h)


def test_chain_batch_single_invalid_item_atomic(deployment, w3):
    """批次中间项金额被篡改 → 整笔回滚：无部分位图、无资金流出。"""
    addr = Web3.to_checksum_address(deployment["address"])
    c = _contract(w3, addr)
    # 使用尚未领取的索引 4,5,6
    items = [{"index": i} for i in (4, 5, 6)]
    bal_contract_before = w3.eth.get_balance(addr)
    bal_user_before = {
        i: w3.eth.get_balance(Web3.to_checksum_address(ALLOCATIONS[i]["account"]))
        for i in (4, 5, 6)
    }

    rcpt = _claim_batch_raw(w3, addr, ANVIL_KEY0, items, tamper_amount_index=1)
    assert rcpt["status"] == 0

    for i in (4, 5, 6):
        assert c.functions.isClaimed(i).call() is False
        assert w3.eth.get_balance(Web3.to_checksum_address(ALLOCATIONS[i]["account"])) == bal_user_before[i]
    assert w3.eth.get_balance(addr) == bal_contract_before


def test_chain_batch_duplicate_index_atomic(deployment, w3):
    """同一笔批次内索引重复 → 第二项标记时回滚。"""
    addr = deployment["address"]
    c = _contract(w3, addr)
    # 用未领取的 7，然后重复 7
    norm_alloc = [dict(a, amount=int(a["amount"])) for a in ALLOCATIONS]
    tree, norm = build_tree_from_allocations(norm_alloc, w3.eth.chain_id, addr)
    acct = w3.eth.account.from_key(ANVIL_KEY0)

    leaf7 = encode_leaf(w3.eth.chain_id, addr, 7, norm[7]["account"], norm[7]["amount"])
    p7 = [b for b in tree.proof(leaf7)]

    fn = c.functions.claimBatch(
        [7, 7],
        [Web3.to_checksum_address(norm[7]["account"])] * 2,
        [norm[7]["amount"]] * 2,
        [p7, p7],
    )
    tx = fn.build_transaction({"from": acct.address, "value": 0,
                               "gas": 3_000_000, "chainId": w3.eth.chain_id,
                               "nonce": w3.eth.get_transaction_count(acct.address)})
    h = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h)
    assert rcpt["status"] == 0
    assert c.functions.isClaimed(7).call() is False


def test_cross_contract_replay(deployment, w3):
    """同链部署第二个实例（独立根），实例 A 的证明在实例 B 必须失败。"""
    from deploy.deploy import deploy, load_allocations

    out = REPO_ROOT / "deployments" / "anvil-test-b.json"
    info_b = deploy(w3.provider.endpoint_uri, ANVIL_KEY0,
                    load_allocations(REPO_ROOT / "examples" / "allocations.json"),
                    token=ZERO, fund_eth=20.0, out_file=out)
    addr_a, addr_b = deployment["address"], info_b["address"]
    assert addr_a != addr_b

    # 用绑定 A 的叶子+证明去 B 领取
    chain_id = w3.eth.chain_id
    tree_a, norm = build_tree_from_allocations(
        [{"index": int(a["index"]), "account": a["account"], "amount": int(a["amount"])}
         for a in ALLOCATIONS], chain_id, addr_a)
    cB = _contract(w3, addr_b)
    acct = w3.eth.account.from_key(ANVIL_KEY0)
    a0 = norm[0]
    leaf = encode_leaf(chain_id, addr_a, 0, a0["account"], a0["amount"])
    proof = [b for b in tree_a.proof(leaf)]

    fn = cB.functions.claim(0, a0["account"], a0["amount"], proof)
    tx = fn.build_transaction({"from": acct.address, "value": 0,
                               "gas": 1_000_000, "chainId": chain_id,
                               "nonce": w3.eth.get_transaction_count(acct.address)})
    h = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h)
    assert rcpt["status"] == 0
    assert cB.functions.isClaimed(0).call() is False


# --------------------------------------------------------------------------
# 跨链重放：另起 chainId 不同的 Anvil，重新部署并验证旧证明失效
# --------------------------------------------------------------------------

def _wait_rpc(url, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                        "method": "eth_chainId", "params": []}, timeout=1).ok:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    raise RuntimeError("anvil timeout")


def test_cross_chain_replay(forge_build):
    if not shutil.which("anvil"):
        pytest.skip("anvil missing")
    import socket
    port = 8547
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            pytest.skip(f"port {port} busy")
    proc = subprocess.Popen(
        ["anvil", "--port", str(port), "--silent", "--chain-id", "5555"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url = f"http://127.0.0.1:{port}"
        _wait_rpc(url)
        from deploy.deploy import deploy, load_allocations
        info = deploy(url, ANVIL_KEY0,
                      load_allocations(REPO_ROOT / "examples" / "allocations.json"),
                      token=ZERO, fund_eth=10.0,
                      out_file=REPO_ROOT / "deployments" / "anvil-test-chain5555.json")

        w3 = Web3(Web3.HTTPProvider(url))
        assert w3.eth.chain_id == 5555
        c = _contract(w3, info["address"])

        # 用 chainId=31337（部署 A 的链）构造叶子与证明，发到 5555 链
        tree_wrong, norm = build_tree_from_allocations(
            [{"index": int(a["index"]), "account": a["account"], "amount": int(a["amount"])}
             for a in ALLOCATIONS], 31337, info["address"])
        a0 = norm[0]
        leaf = encode_leaf(31337, info["address"], 0, a0["account"], a0["amount"])
        proof = [b for b in tree_wrong.proof(leaf)]

        acct = w3.eth.account.from_key(ANVIL_KEY0)
        fn = c.functions.claim(0, a0["account"], a0["amount"], proof)
        tx = fn.build_transaction({"from": acct.address, "value": 0,
                                   "gas": 1_000_000, "chainId": 5555,
                                   "nonce": w3.eth.get_transaction_count(acct.address)})
        h = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
        rcpt = w3.eth.wait_for_transaction_receipt(h)
        # 要么 deployChainId 检查拦截，要么叶子 chainId 不符 → 必然回滚
        assert rcpt["status"] == 0
        assert c.functions.isClaimed(0).call() is False

        # 用正确 chainId 的证明则成功（证明后端是按当前链生成的）
        tree_ok, _ = build_tree_from_allocations(
            [{"index": int(a["index"]), "account": a["account"], "amount": int(a["amount"])}
             for a in ALLOCATIONS], 5555, info["address"])
        leaf_ok = encode_leaf(5555, info["address"], 0, a0["account"], a0["amount"])
        proof_ok = [b for b in tree_ok.proof(leaf_ok)]
        fn2 = c.functions.claim(0, a0["account"], a0["amount"], proof_ok)
        tx2 = fn2.build_transaction({"from": acct.address, "value": 0,
                                     "gas": 1_000_000, "chainId": 5555,
                                     "nonce": w3.eth.get_transaction_count(acct.address)})
        h2 = w3.eth.send_raw_transaction(acct.sign_transaction(tx2).raw_transaction)
        assert w3.eth.wait_for_transaction_receipt(h2)["status"] == 1
        assert c.functions.isClaimed(0).call() is True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
