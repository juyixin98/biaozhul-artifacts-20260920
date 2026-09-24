"""端到端集成测试：HTTP API -> FastAPI -> web3.py -> 本地 Anvil 合约。

覆盖验收点:
- 签名乱序仍达到阈值并调度
- 同一签名人重复签名只计一次（链上/服务两层）
- 达到阈值后仍需等待延时才能执行
- 目标失败按 retryCooldown 明确规则重试，成功后至多执行一次
- nonce 重放被拒、操作参数绑定（换 data 签名无效）
- 签名人变更走多签自调用，变更后旧签名人失效
- 执行回调返回数据
"""
from __future__ import annotations

import json

import pytest

from tests.conftest import evm_increase_time, ROOT


def _signers(ctx):
    return ctx["chain"]["deployment"]["signers"]


def _propose_increment(http):
    r = http.post("/actions/increment", json={})
    assert r.status_code == 200, r.text
    return r.json()


def _collect_and_approve(http, op, signers, subset=None):
    chosen = subset or signers
    for addr in chosen:
        r = http.post(f"/operations/{op['op_hash']}/sign", json={
            "signer": addr, "nonce": op["nonce"], "validUntil": op["valid_until"],
        })
        assert r.status_code == 200, r.text
    r = http.post(f"/operations/{op['op_hash']}/approve", json={
        "nonce": op["nonce"], "validUntil": op["valid_until"],
    })
    assert r.status_code == 200, r.text
    return r.json()


def _execute(http, op, expect_code=200):
    r = http.post(f"/operations/{op['op_hash']}/execute", json={
        "nonce": op["nonce"], "validUntil": op["valid_until"],
    })
    assert r.status_code == expect_code, r.text
    return r.json()


# ----------------------------------------------------------------------
# 0. 基础连通与状态
# ----------------------------------------------------------------------

def test_00_health_and_state(client):
    c = client["http"]
    h = c.get("/health").json()
    assert h["connected"] is True
    assert h["chain_id"] == 31337  # Anvil 默认链 ID

    st = c.get("/state").json()
    assert st["onchain_threshold"] == 2
    assert len(st["signers"]) == 4
    assert st["delay_seconds"] == 2
    assert st["retry_cooldown_seconds"] == 2


# ----------------------------------------------------------------------
# 1. 乱序签名 -> 调度 -> 延时 -> 成功执行一次
# ----------------------------------------------------------------------

def test_01_out_of_order_signatures_and_execute_once(client):
    http = client["http"]
    svc = client["svc"]
    signers = _signers(client)

    op = _propose_increment(http)
    # 乱序：3 号先签，1 号后签（阈值 2）
    body = _collect_and_approve(http, op, signers, subset=[signers[2], signers[0]])

    assert body["tx"]["status"] == "success"
    assert body["onchain"]["scheduled"] is True
    assert body["onchain"]["approvalCount"] == 2
    ready_at = body["onchain"]["readyAt"]
    assert ready_at > svc.now() - 1

    # 延时未到：执行交易 revert（TimelockNotReady）
    early = _execute(http, op)
    assert early["tx"]["status"] == "reverted"
    assert "TimelockNotReady" in early["tx"]["error"]

    # 等过延时
    evm_increase_time(svc.w3, 3)
    ok = _execute(http, op)
    assert ok["tx"]["status"] == "success"
    assert ok["outcome"] == "executed"
    assert ok["counter"] == 1
    assert ok["onchain"]["executed"] is True

    # 再执行 -> AlreadyExecuted，目标计数不增加
    again = _execute(http, op)
    assert again["tx"]["status"] == "reverted"
    assert "AlreadyExecuted" in again["tx"]["error"]
    assert svc.counter_value() == 1


# ----------------------------------------------------------------------
# 2. 重复签名去重（同一签名人签多次，只计一次，不触发调度）
# ----------------------------------------------------------------------

def test_02_duplicate_signatures_deduplicated(client):
    http = client["http"]
    signers = _signers(client)
    op = _propose_increment(http)

    # s1 签三次
    for _ in range(3):
        r = http.post(f"/operations/{op['op_hash']}/sign", json={
            "signer": signers[0], "nonce": op["nonce"], "validUntil": op["valid_until"],
        })
        assert r.status_code == 200
        assert r.json()["collected"] == [signers[0].lower()]

    body = _collect_and_approve(http, op, [signers[0]])
    # 只提交了同一签名人的多份签名：计数仍为 1，未调度
    assert body["onchain"]["approvalCount"] == 1
    assert body["onchain"]["scheduled"] is False

    # 非签名人持有的地址不能通过 /sign
    r = http.post(f"/operations/{op['op_hash']}/sign", json={
        "signer": "0x000000000000000000000000000000000000dEaD",
        "nonce": op["nonce"], "validUntil": op["valid_until"],
    })
    assert r.status_code == 403


# ----------------------------------------------------------------------
# 3. 阈值签名可跨多次 approve 收集（链上累积去重）
# ----------------------------------------------------------------------

def test_03_approve_twice_then_schedule(client):
    http = client["http"]
    signers = _signers(client)
    op = _propose_increment(http)

    # 第一次只交 s2
    r = http.post(f"/operations/{op['op_hash']}/sign", json={
        "signer": signers[1], "nonce": op["nonce"], "validUntil": op["valid_until"]})
    assert r.status_code == 200
    r = http.post(f"/operations/{op['op_hash']}/approve",
                  json={"nonce": op["nonce"], "validUntil": op["valid_until"],
                        "signers": [signers[1]]})
    assert r.json()["onchain"]["approvalCount"] == 1
    assert r.json()["onchain"]["scheduled"] is False

    # 第二次再交 s2（重复，不增加）+ s3（新增，触发调度）
    r = http.post(f"/operations/{op['op_hash']}/sign", json={
        "signer": signers[2], "nonce": op["nonce"], "validUntil": op["valid_until"]})
    assert r.status_code == 200
    r = http.post(f"/operations/{op['op_hash']}/approve",
                  json={"nonce": op["nonce"], "validUntil": op["valid_until"],
                        "signers": [signers[1], signers[2]]})
    assert r.json()["onchain"]["approvalCount"] == 2
    assert r.json()["onchain"]["scheduled"] is True


# ----------------------------------------------------------------------
# 4. 目标失败 -> 冷却规则 -> 冷却后重试成功；全程至多成功一次
#    使用独立的“前 N 秒失败”的 Counter 部署
# ----------------------------------------------------------------------

@pytest.fixture()
def flaky_client(chain):
    """额外部署一个在部署后 8 秒前失败、之后成功的 Counter。"""
    from fastapi.testclient import TestClient
    from app.chain import WalletService, load_abi
    from app.main import create_app, store
    from web3 import Web3

    dep = chain["deployment"]
    w3 = chain["w3"]
    deployer = w3.eth.account.from_key(chain["deployment"]["signer_keys"][0])
    now = w3.eth.get_block("latest")["timestamp"]

    Counter = w3.eth.contract(abi=load_abi("Counter"), bytecode=json.loads(
        (ROOT / "contracts" / "out" / "Counter.sol" / "Counter.json")
        .read_text())["bytecode"]["object"])
    tx = Counter.constructor(dep["wallet"], now + 8).build_transaction({
        "from": deployer.address,
        "nonce": w3.eth.get_transaction_count(deployer.address),
        "gas": 3_000_000, "gasPrice": w3.eth.gas_price, "chainId": w3.eth.chain_id,
    })
    rc = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(deployer.sign_transaction(tx).raw_transaction))
    flaky = rc["contractAddress"]

    svc = WalletService.from_deployment(chain["dep_file"])
    svc.counter_address = Web3.to_checksum_address(flaky)
    application = create_app(svc)
    store.ops.clear()
    with TestClient(application) as c:
        yield {"http": c, "svc": svc, "chain": chain}


def test_04_failure_cooldown_retry_success_once(flaky_client):
    http = flaky_client["http"]
    svc = flaky_client["svc"]
    signers = svc.signers

    op = _propose_increment(http)
    _collect_and_approve(http, op, [signers[0], signers[2]])

    # 延时 2 秒未到
    r = _execute(http, op)
    assert r["tx"]["status"] == "reverted"
    assert "TimelockNotReady" in r["tx"]["error"]

    # +3s：延时已到，但目标仍失败（< 部署+8），事件标记 failed_retryable
    evm_increase_time(svc.w3, 3)
    failed = _execute(http, op)
    assert failed["tx"]["status"] == "success"  # execute 本身不 revert
    assert failed["outcome"] == "failed_retryable"
    assert failed["onchain"]["executed"] is False
    assert failed["next_try_after"] is not None
    assert failed["counter"] == 0

    # 不推进时间立即重试 -> RetryCooldownActive
    too_soon = _execute(http, op)
    assert too_soon["tx"]["status"] == "reverted"
    assert "RetryCooldownActive" in too_soon["tx"]["error"]

    # 再 +5s（累计 +8s）：冷却已过且目标恢复 -> 成功
    evm_increase_time(svc.w3, 5)
    ok = _execute(http, op)
    assert ok["tx"]["status"] == "success"
    assert ok["outcome"] == "executed"
    assert ok["counter"] == 1
    assert ok["onchain"]["executed"] is True

    # 再执行 -> AlreadyExecuted（至多成功一次）
    evm_increase_time(svc.w3, 3)
    again = _execute(http, op)
    assert again["tx"]["status"] == "reverted"
    assert "AlreadyExecuted" in again["tx"]["error"]
    assert svc.counter_value() == 1


# ----------------------------------------------------------------------
# 5. nonce 防重放：同一 nonce 再次 approve 被拒
# ----------------------------------------------------------------------

def test_05_nonce_replay_rejected(client):
    http = client["http"]
    svc = client["svc"]
    signers = _signers(client)

    op1 = _propose_increment(http)
    _collect_and_approve(http, op1, [signers[0], signers[1]])

    # 手工构造相同 nonce 但（可能）不同参数的第二个提案
    r = http.post("/operations", json={
        "target": svc.counter_address, "data": "0xd09de08a",
        "nonce": op1["nonce"], "validUntil": 0,
    })
    assert r.status_code == 200
    op2 = r.json()
    for a in [signers[0], signers[1]]:
        rr = http.post(f"/operations/{op2['op_hash']}/sign", json={
            "signer": a, "nonce": op2["nonce"], "validUntil": 0})
        assert rr.status_code == 200
    rr = http.post(f"/operations/{op2['op_hash']}/approve",
                   json={"nonce": op2["nonce"], "validUntil": 0})
    # 交易被拒（NonceAlreadyUsed）
    assert rr.status_code == 200
    assert rr.json()["tx"]["status"] == "reverted"
    assert "NonceAlreadyUsed" in rr.json()["tx"]["error"]


# ----------------------------------------------------------------------
# 6. 签名人变更：多签自调用 configureSigners，旧签名人立即失效
# ----------------------------------------------------------------------

def test_06_signer_change_via_multisig(client):
    http = client["http"]
    svc = client["svc"]
    dep = client["chain"]["deployment"]
    signers = dep["signers"]

    # 新集合：s1、s2、s4（移除 s3，加入 Anvil 第 4 个测试账户），阈值 2
    s4 = svc.w3.eth.account.from_key(dep["signer_keys"][3]).address
    new_set = [signers[0], signers[1], s4]

    r = http.post("/signers/change", json={"signers": new_set, "threshold": 2})
    assert r.status_code == 200, r.text
    op = r.json()

    # 变更提案必须由旧集合（s2+s3）签名批准
    for a in [signers[1], signers[2]]:
        rr = http.post(f"/operations/{op['op_hash']}/sign", json={
            "signer": a, "nonce": op["nonce"], "validUntil": op["valid_until"]})
        assert rr.status_code == 200, rr.text
    rr = http.post(f"/operations/{op['op_hash']}/approve",
                   json={"nonce": op["nonce"], "validUntil": op["valid_until"]})
    assert rr.json()["tx"]["status"] == "success"

    # 延时未到执行变更 -> revert
    rr = http.post(f"/operations/{op['op_hash']}/execute",
                   json={"nonce": op["nonce"], "validUntil": op["valid_until"]})
    assert "TimelockNotReady" in rr.json()["tx"]["error"]

    evm_increase_time(svc.w3, 3)
    rr = http.post(f"/operations/{op['op_hash']}/execute",
                   json={"nonce": op["nonce"], "validUntil": op["valid_until"]})
    assert rr.json()["outcome"] == "executed"

    # 链上签名人已变更
    onchain = svc.signers
    assert s4.lower() in [a.lower() for a in onchain]
    assert signers[2].lower() not in [a.lower() for a in onchain]

    # /state 反映新集合
    st = http.get("/state").json()
    assert s4.lower() in [a.lower() for a in st["signers"]]

    # 旧签名人 s3 对新操作签名上链 -> InvalidSignature
    op2 = _propose_increment(http)
    rr = http.post(f"/operations/{op2['op_hash']}/sign", json={
        "signer": signers[2], "nonce": op2["nonce"], "validUntil": op2["valid_until"]})
    # 服务层 is_signer 已为 False
    assert rr.status_code == 403


# ----------------------------------------------------------------------
# 7. 操作哈希绑定参数：替换 calldata 后签名无效
# ----------------------------------------------------------------------

def test_07_signature_bound_to_calldata(client):
    http = client["http"]
    svc = client["svc"]
    signers = _signers(client)

    # 对 echo(42) 签名
    counter = svc.w3.eth.contract(address=svc.counter_address, abi=load_abi_local())
    data42 = counter.encode_abi("echo", [42])
    data99 = counter.encode_abi("echo", [99])

    r = http.post("/operations", json={
        "target": svc.counter_address, "data": data42, "nonce": 100})
    op42 = r.json()
    for a in [signers[0], signers[1]]:
        rr = http.post(f"/operations/{op42['op_hash']}/sign", json={
            "signer": a, "nonce": 100, "validUntil": 0})
        assert rr.status_code == 200

    # 另提一个 data=echo(99) 同 nonce 的操作，复用前面的签名字节直接上链 approve
    r = http.post("/operations", json={
        "target": svc.counter_address, "data": data99, "nonce": 101})
    op99 = r.json()
    rec = http.get(f"/operations/{op42['op_hash']}").json()["proposal"]
    sigs = list(rec["signatures"].values())

    func = svc.contract.functions.approve(
        svc.counter_address,
        bytes.fromhex(data99.removeprefix("0x")),
        0, 101, 0,
        [bytes.fromhex(s.removeprefix("0x")) for s in sigs],
    )
    result = svc.send_function(signers[0], func)
    # 摘要不同 -> 恢复出的地址与签名人不符 -> InvalidSignature
    assert result["status"] == "reverted"
    assert "InvalidSignature" in result["error"]


def load_abi_local():
    from app.chain import load_abi
    return load_abi("Counter")
