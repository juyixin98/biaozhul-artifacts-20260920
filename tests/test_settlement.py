"""验收场景：两次部分成交舍入、取消竞争、重放、转账失败、到期、费用上限、
域名绑定、非法签名，以及双方资产守恒 / 成交量不超过签名额度。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from web3 import Web3

from app.signing import ORDER_KEYS, order_to_tuple, sign_order as eip712_sign_order

from .conftest import assert_conserved, balances, fill, sign_order


def test_health(svc):
    r = svc["client"].get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["chain_id"] == 31337
    assert body["block_number"] >= 0


# ---- 1. 两次部分成交舍入 + 末笔补足 + 守恒 + 额度边界 ----

def test_two_partial_fills_rounding(svc):
    client = svc["client"]
    signed = sign_order(client, sell_amount=3, buy_amount=100, nonce=101)
    before = balances(svc)

    buys = []
    for amount in (1, 1, 1):
        r = fill(client, signed, amount)
        assert r.status_code == 200, r.text
        buys.append(r.json()["filled"]["buyPaid"])

    assert buys == [33, 33, 34]  # 两次部分向下取整，末笔补足
    after = balances(svc)
    assert_conserved(before, after)
    assert after["A"]["taker"] - before["A"]["taker"] == 3           # taker 恰好拿到 3
    assert before["A"]["maker"] - after["A"]["maker"] == 3           # maker 恰好付出 3
    assert after["B"]["maker"] - before["B"]["maker"] == 100         # maker 恰好收到签名额度
    assert before["B"]["taker"] - after["B"]["taker"] == 100         # taker 恰好付出 100

    st = client.get(f"/orders/{signed['order_hash']}").json()
    assert st["filledSellAmount"] == 3 <= signed["order"]["sellAmount"]
    assert st["filledBuyAmount"] == 100 <= signed["order"]["buyAmount"]


def test_partial_fills_never_exceed_quota(svc):
    """零散比例 7->13，多笔部分成交累计不得超过签名额度。"""
    client = svc["client"]
    signed = sign_order(client, sell_amount=7, buy_amount=13, nonce=102)
    before = balances(svc)
    buys = []
    for amount in (2, 2, 3):
        r = fill(client, signed, amount)
        assert r.status_code == 200, r.text
        buys.append(r.json()["filled"]["buyPaid"])
    assert buys == [3, 3, 7]
    after = balances(svc)
    assert_conserved(before, after)
    assert after["B"]["maker"] - before["B"]["maker"] == 13  # 不超过且恰好等于签名 buyAmount

    # 再填即超额度 -> 409
    r = fill(client, signed, 1)
    assert r.status_code == 409
    assert "FillExceedsRemaining" in r.json()["detail"]


def test_overfill_single_fill_rejected(svc):
    client = svc["client"]
    signed = sign_order(client, sell_amount=100, buy_amount=100, nonce=103)
    r = fill(client, signed, 101)
    assert r.status_code == 409
    assert "FillExceedsRemaining" in r.json()["detail"]


# ---- 2. 取消竞争 ----

def test_cancel_then_fill_loses(svc):
    """maker 先取消 nonce，taker 后到的成交被拒。"""
    client = svc["client"]
    signed = sign_order(client, sell_amount=100, buy_amount=200, nonce=201)
    r = client.post("/cancellations", json={"nonce": 201})
    assert r.status_code == 200
    r = fill(client, signed, 50)
    assert r.status_code == 409
    assert "NonceAlreadyCancelled" in r.json()["detail"]
    st = client.get(f"/orders/{signed['order_hash']}").json()
    assert st["filledSellAmount"] == 0


def test_fill_then_cancel_preserves_partial(svc):
    """先部分成交，再取消：已成交部分保留，后续成交被拒。"""
    client = svc["client"]
    signed = sign_order(client, sell_amount=100, buy_amount=200, nonce=202)
    before = balances(svc)
    assert fill(client, signed, 40).status_code == 200
    assert client.post("/cancellations", json={"nonce": 202}).status_code == 200
    r = fill(client, signed, 60)
    assert r.status_code == 409
    assert "NonceAlreadyCancelled" in r.json()["detail"]
    st = client.get(f"/orders/{signed['order_hash']}").json()
    assert st["filledSellAmount"] == 40
    after = balances(svc)
    assert_conserved(before, after)
    assert after["A"]["taker"] - before["A"]["taker"] == 40


# ---- 3. 重放 ----

def test_replay_same_signature_rejected(svc):
    client = svc["client"]
    signed = sign_order(client, sell_amount=100, buy_amount=100, nonce=301)
    assert fill(client, signed, 100).status_code == 200
    # 同一签名重放
    r = fill(client, signed, 100)
    assert r.status_code == 409
    assert "FillExceedsRemaining" in r.json()["detail"]


# ---- 4. 转账失败 ----

def test_sell_token_transfer_failure_rolls_back(svc):
    client, s = svc["client"], svc["state"]
    signed = sign_order(client, sell_amount=100, buy_amount=100, nonce=401)
    before = balances(svc)

    # 打开 TKA 转账失败开关（任何人可切换的模拟开关）
    w3 = svc["w3"]
    pk = svc["settings"].deployer_pk
    acct = w3.eth.account.from_key(pk)
    tx = s.token_a.functions.setTransfersFail(True).build_transaction(
        {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address), "chainId": w3.eth.chain_id})
    h = w3.eth.send_raw_transaction(w3.eth.account.sign_transaction(tx, pk).raw_transaction)
    w3.eth.wait_for_transaction_receipt(h)

    try:
        r = fill(client, signed, 100)
        assert r.status_code == 409
        assert "TransferFailed" in r.json()["detail"]
    finally:
        tx = s.token_a.functions.setTransfersFail(False).build_transaction(
            {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address), "chainId": w3.eth.chain_id})
        h = w3.eth.send_raw_transaction(w3.eth.account.sign_transaction(tx, pk).raw_transaction)
        w3.eth.wait_for_transaction_receipt(h)

    st = client.get(f"/orders/{signed['order_hash']}").json()
    assert st == {"filledSellAmount": 0, "filledBuyAmount": 0, "filledFeeAmount": 0}
    assert balances(svc) == before  # 双方余额完全不变


def test_insufficient_balance_transfer_failure(svc):
    """maker 余额不足时 transferFrom revert，整体回滚。"""
    client = svc["client"]
    signed = sign_order(client, sell_amount=10**30, buy_amount=1, nonce=402)
    before = balances(svc)
    r = fill(client, signed, 10**30)
    assert r.status_code == 409
    assert "TransferFailed" in r.json()["detail"]
    assert balances(svc) == before


# ---- 5. 到期 ----

def test_expired_order_rejected(svc):
    client = svc["client"]
    signed = sign_order(
        client, sell_amount=10, buy_amount=10, nonce=501, expiry=int(time.time()) - 3600
    )
    r = fill(client, signed, 5)
    assert r.status_code == 409
    assert "OrderExpired" in r.json()["detail"]


# ---- 6. 费用上限（累计）+ 费用守恒 ----

def test_fee_cap_cumulative_and_conservation(svc):
    client = svc["client"]
    signed = sign_order(client, sell_amount=100, buy_amount=100, fee_cap=10, nonce=601)
    before = balances(svc)

    assert fill(client, signed, 50, fee=5).status_code == 200
    # 6 使累计 11 > 10
    r = fill(client, signed, 50, fee=6)
    assert r.status_code == 409
    assert "FeeCapExceeded" in r.json()["detail"]
    # 5 正好打满，成交成功
    r = fill(client, signed, 50, fee=5)
    assert r.status_code == 200, r.text

    after = balances(svc)
    assert_conserved(before, after)
    assert after["B"]["fee"] - before["B"]["fee"] == 10
    # maker 收 100，feeRecv 收 10，taker 共出 110
    assert after["B"]["maker"] - before["B"]["maker"] == 100
    assert before["B"]["taker"] - after["B"]["taker"] == 110


# ---- 7. 签名域绑定链与合约 ----

def test_signature_bound_to_chain_and_contract(svc):
    """为当前合约签的名，在第二个新部署的合约上必须被拒（DOMAIN_SEPARATOR 不同）。"""
    s = svc["state"]
    w3 = svc["w3"]
    art = json.loads((Path(__file__).resolve().parent.parent
                      / "out" / "BoundedSettlement.sol" / "BoundedSettlement.json").read_text())
    Settlement = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    pk = svc["settings"].deployer_pk
    acct = w3.eth.account.from_key(pk)
    tx = Settlement.constructor(s.settings.fee_receiver).build_transaction(
        {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address), "chainId": w3.eth.chain_id})
    h = w3.eth.send_raw_transaction(w3.eth.account.sign_transaction(tx, pk).raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h)
    settlement2 = w3.eth.contract(address=rcpt.contractAddress, abi=art["abi"])

    # 两个域必须不同
    assert settlement2.functions.DOMAIN_SEPARATOR().call() != \
        s.settlement.functions.DOMAIN_SEPARATOR().call()

    signed = sign_order(svc["client"], sell_amount=100, buy_amount=100, nonce=701)
    order_tuple = order_to_tuple(signed["order"])

    # taker 在第二个合约上用同一签名结算 -> 估算时即回滚（recovered != maker）
    taker_acct = w3.eth.account.from_key(s.settings.taker_pk)
    from web3.exceptions import ContractCustomError

    with pytest.raises(ContractCustomError) as ei:
        settlement2.functions.fillOrder(
            order_tuple, 100, 0, signed["signature"]
        ).estimate_gas({"from": taker_acct.address})
    # 0x42d750dc == InvalidSignature(address,address) 选择子
    assert "0x42d750dc" in str(ei.value).lower(), ei.value


def test_local_eip712_digest_matches_chain(svc):
    """本地 eth_account 签名的订单摘要必须等于链上 hashOrder（域名对拍）。"""
    signed = sign_order(svc["client"], sell_amount=123, buy_amount=456, nonce=702)
    onchain = svc["state"].settlement.functions.hashOrder(order_to_tuple(signed["order"])).call()
    assert onchain.hex() == signed["order_hash"]
    assert len(signed["signature"]) == 132  # 0x + 65 字节


def test_wrong_signer_rejected(svc):
    """taker 的私钥签 maker 的订单 -> InvalidSignature。"""
    client, s = svc["client"], svc["state"]
    order_body = sign_order(client, sell_amount=100, buy_amount=100, nonce=703)["order"]
    bad_sig = eip712_sign_order(s.settings.taker_pk, s.settings.chain_id, s.settings.settlement, order_body)
    r = client.post("/settlements", json={
        "order": order_body, "signature": bad_sig, "sell_fill_amount": 50, "fee_amount": 0})
    assert r.status_code == 409
    assert "InvalidSignature" in r.json()["detail"]


def test_tampered_order_rejected(svc):
    """签名后改价格 -> 摘要不符，被拒。"""
    client = svc["client"]
    signed = sign_order(client, sell_amount=100, buy_amount=100, nonce=704)
    tampered = dict(signed["order"])
    tampered["buyAmount"] = 1
    r = client.post("/settlements", json={
        "order": tampered, "signature": signed["signature"],
        "sell_fill_amount": 100, "fee_amount": 0})
    assert r.status_code == 409
    assert "InvalidSignature" in r.json()["detail"]
