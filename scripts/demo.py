"""端到端示例：通过 FastAPI HTTP 路由完成签名/部分成交/取消/重放/失败路径，
并在每步用 web3 直查余额做双方资产守恒校验。

用法（先启动 Anvil 并部署，见 README）：
    python scripts/demo.py
"""
from __future__ import annotations

import os
import sys
import time

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

ORDER_TUPLE_KEYS = (
    "maker", "sellToken", "buyToken", "sellAmount",
    "buyAmount", "feeCap", "nonce", "expiry",
)


def main() -> None:
    settings = Settings.load()
    app = create_app(settings)

    with TestClient(app) as client:
        svc = app.state.svc
        w3, token_a, token_b, settlement = svc.w3, svc.token_a, svc.token_b, svc.settlement
        MAKER, TAKER, FEE = settings.maker, settings.taker, settings.fee_receiver

        def balances():
            return {
                "A": (
                    token_a.functions.balanceOf(MAKER).call(),
                    token_a.functions.balanceOf(TAKER).call(),
                    token_a.functions.balanceOf(FEE).call(),
                ),
                "B": (
                    token_b.functions.balanceOf(MAKER).call(),
                    token_b.functions.balanceOf(TAKER).call(),
                    token_b.functions.balanceOf(FEE).call(),
                ),
            }

        def assert_conserved(before, after, tag):
            for tok in ("A", "B"):
                assert sum(before[tok]) == sum(after[tok]), f"{tag}: {tok} 不守恒"
            print(f"  [守恒] {tag}: A 总量={sum(after['A'])}, B 总量={sum(after['B'])}  ✓")

        def step(title):
            print(f"\n=== {title} ===")

        step("0. 健康检查")
        r = client.get("/health").json()
        print(f"  chainId={r['chain_id']} block={r['block_number']} settlement={r['contract']}")
        assert r["chain_id"] == 31337

        step("1. maker 签名订单：3 TKA -> 100 TKB，nonce=42，feeCap=0")
        expiry = int(time.time()) + 3600
        r = client.post(
            "/orders/sign",
            json={
                "signer": "maker",
                "sell_token": "A",
                "buy_token": "B",
                "sell_amount": 3,
                "buy_amount": 100,
                "fee_cap": 0,
                "nonce": 42,
                "expiry": expiry,
            },
        ).json()
        order, sig, oh = r["order"], r["signature"], r["order_hash"]
        print(f"  orderHash=0x{oh}")
        print(f"  signature={sig[:40]}...")
        # EIP-712 对拍：链上 hashOrder 与本地签名摘要一致
        onchain = settlement.functions.hashOrder(
            tuple(order[k] for k in ORDER_TUPLE_KEYS)
        ).call()
        assert onchain.hex() == oh, "本地签名摘要与链上 hashOrder 不一致"
        print("  本地 EIP-712 摘要 == 链上 hashOrder  ✓")

        before = balances()
        results = []
        for i, fill in enumerate((1, 1, 1), start=1):
            step(f"2.{i} 第 {i} 笔部分成交（sell_fill=1）")
            resp = client.post(
                "/settlements",
                json={"order": order, "signature": sig, "sell_fill_amount": fill, "fee_amount": 0},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            buy_paid = body["filled"]["buyPaid"]
            results.append(buy_paid)
            print(
                f"  buyPaid={buy_paid} totalSellFilled={body['filled']['totalSellFilled']} "
                f"tx={body['tx']['txHash'][:18]}..."
            )
        assert results == [33, 33, 34], results
        print("\n  舍入结果 [33, 33, 34]：两次部分成交向下取整，最后一笔补足，合计恰为签名额度 100  ✓")

        after = balances()
        assert_conserved(before, after, "三次部分成交")
        print(f"  maker 净出 {before['A'][0]-after['A'][0]} TKA，净入 {after['B'][0]-before['B'][0]} TKB")
        print(f"  taker 净入 {after['A'][1]-before['A'][1]} TKA，净出 {before['B'][1]-after['B'][1]} TKB")
        st = client.get(f"/orders/{oh}").json()
        assert st["filledSellAmount"] == 3 and st["filledBuyAmount"] == 100
        assert st["filledSellAmount"] <= order["sellAmount"]
        print(f"  链上订单状态: filledSell={st['filledSellAmount']} filledBuy={st['filledBuyAmount']}  ✓")

        step("3. 重放：同一签名再提交一次")
        resp = client.post(
            "/settlements",
            json={"order": order, "signature": sig, "sell_fill_amount": 1, "fee_amount": 0},
        )
        assert resp.status_code == 409, resp.text
        print(f"  HTTP 409: {resp.json()['detail']}  ✓")

        step("4. 取消竞争：新订单 nonce=43，先 cancel 再提交")
        r = client.post("/orders/sign", json={
            "signer": "maker", "sell_amount": 10, "buy_amount": 20, "fee_cap": 0, "nonce": 43,
        }).json()
        order2, sig2 = r["order"], r["signature"]
        cr = client.post("/cancellations", json={"nonce": 43})
        assert cr.status_code == 200, cr.text
        print(f"  cancelNonce tx={cr.json()['tx']['txHash'][:18]}...")
        resp = client.post("/settlements", json={
            "order": order2, "signature": sig2, "sell_fill_amount": 5, "fee_amount": 0})
        assert resp.status_code == 409
        print(f"  HTTP 409: {resp.json()['detail']}  ✓")

        step("5. 到期订单：expiry=一小时前")
        r = client.post("/orders/sign", json={
            "signer": "maker", "sell_amount": 10, "buy_amount": 20, "fee_cap": 0,
            "nonce": 44, "expiry": int(time.time()) - 3600,
        }).json()
        resp = client.post("/settlements", json={
            "order": r["order"], "signature": r["signature"], "sell_fill_amount": 1, "fee_amount": 0})
        assert resp.status_code == 409
        print(f"  HTTP 409: {resp.json()['detail']}  ✓")

        step("6. 费用上限：feeCap=1，本次费=2")
        r = client.post("/orders/sign", json={
            "signer": "maker", "sell_amount": 10, "buy_amount": 20, "fee_cap": 1, "nonce": 45,
        }).json()
        resp = client.post("/settlements", json={
            "order": r["order"], "signature": r["signature"], "sell_fill_amount": 5, "fee_amount": 2})
        assert resp.status_code == 409
        print(f"  HTTP 409: {resp.json()['detail']}  ✓")

        step("7. 转账失败：打开 TKA 转账失败开关后结算必须回滚")
        acct = w3.eth.account.from_key(settings.deployer_pk)

        def flip(value: bool):
            tx = token_a.functions.setTransfersFail(value).build_transaction(
                {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
                 "chainId": w3.eth.chain_id})
            h = w3.eth.send_raw_transaction(
                w3.eth.account.sign_transaction(tx, settings.deployer_pk).raw_transaction)
            w3.eth.wait_for_transaction_receipt(h)

        flip(True)
        r = client.post("/orders/sign", json={
            "signer": "maker", "sell_amount": 10, "buy_amount": 20, "fee_cap": 0, "nonce": 46,
        }).json()
        oh46 = r["order_hash"]
        before7 = balances()
        resp = client.post("/settlements", json={
            "order": r["order"], "signature": r["signature"], "sell_fill_amount": 5, "fee_amount": 0})
        assert resp.status_code == 409
        print(f"  HTTP 409: {resp.json()['detail']}  ✓")
        st = client.get(f"/orders/{oh46}").json()
        assert st == {"filledSellAmount": 0, "filledBuyAmount": 0, "filledFeeAmount": 0}
        assert balances() == before7
        print("  回滚后 filled=0 且全部余额不变  ✓")
        flip(False)

        print("\n所有示例场景通过。")


if __name__ == "__main__":
    main()
