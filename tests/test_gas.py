"""Gas 增长对比：二分查找 vs 线性扫描，随检查点历史长度变化。

通过 eth_call 的 estimate_gas 读取查询 gas（纯 view 调用，不上链），
并把数值表写入 target/gas-report.json 供 README 引用。
"""
from __future__ import annotations

import json
import pathlib

import pytest
from web3 import Web3

from app.config import DEFAULT_ANVIL_PRIVATE_KEY

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _deploy_named(w3: Web3, name: str) -> object:
    path = ROOT / "out" / f"{name}.sol" / f"{name}.json"
    artifact = json.loads(path.read_text())
    contract = w3.eth.contract(
        abi=artifact["abi"], bytecode=artifact["bytecode"]["object"]
    )
    acct = w3.eth.account.from_key(DEFAULT_ANVIL_PRIVATE_KEY)
    tx = contract.constructor().build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 3_000_000,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    signed = acct.sign_transaction(tx)
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(signed.raw_transaction), timeout=30
    )
    return w3.eth.contract(address=receipt["contractAddress"], abi=artifact["abi"])


def _set_value(w3: Web3, contract, value: int, acct=None) -> None:
    if acct is None:
        acct = w3.eth.account.from_key(DEFAULT_ANVIL_PRIVATE_KEY)
    tx = contract.functions.setValue(value).build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 300_000,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    signed = acct.sign_transaction(tx)
    w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(signed.raw_transaction), timeout=30
    )


def _build_history(w3: Web3, contract, n: int, start_block: int) -> list[int]:
    """在 n 个不同区块各写一个检查点（交易块间隔 2），返回检查点区块号。

    Anvil 语义：交易在「最新块 + 1」打包。要让第一笔进入 start_block，
    先挖至 start_block - 1；此后每两个交易块之间留一个空隙块。
    """
    acct = w3.eth.account.from_key(DEFAULT_ANVIL_PRIVATE_KEY)
    gap = start_block - 1 - w3.eth.block_number
    if gap > 0:
        w3.provider.make_request("evm_mine", [{"blocks": gap}])

    blocks = []
    for i in range(1, n + 1):
        if i > 1:
            w3.provider.make_request("evm_mine", [{"blocks": 1}])  # 空隙块
        _set_value(w3, contract, i, acct=acct)
        blocks.append(w3.eth.block_number)
    return blocks


@pytest.mark.parametrize("n", [16, 64, 256])
def test_query_gas_growth(w3, n: int) -> None:
    binary = _deploy_named(w3, "Checkpoints")
    naive = _deploy_named(w3, "NaiveCheckpoints")

    start_binary = w3.eth.block_number + 10
    b_blocks = _build_history(w3, binary, n, start_binary)
    start_naive = w3.eth.block_number + 10
    n_blocks = _build_history(w3, naive, n, start_naive)
    # 查询点：最老的检查点（线性最坏情况）
    oldest_b, oldest_n = b_blocks[0], n_blocks[0]

    # 预热存储槽（各调一次，模拟真实访问），再 estimate
    binary.functions.valueAt(oldest_b).call()
    naive.functions.valueAt(oldest_n).call()

    gas_binary = binary.functions.valueAt(oldest_b).estimate_gas()
    gas_naive = naive.functions.valueAt(oldest_n).estimate_gas()

    print(f"\nn={n:4d}  binary={gas_binary:6d}  linear={gas_naive:6d}  "
          f"ratio={gas_naive / gas_binary:5.2f}x")

    # 二分必须比线性便宜（n>=16 时）
    assert gas_binary < gas_naive

    # 收集到的结果持久化（parametrize 的最后一个 n 写全量报告）
    report_path = ROOT / "target" / "gas-report.json"
    report_path.parent.mkdir(exist_ok=True)
    row = {"n": n, "binary": gas_binary, "linear": gas_naive}
    data = []
    if report_path.exists():
        data = json.loads(report_path.read_text())
    data = [r for r in data if r["n"] != n]
    data.append(row)
    data.sort(key=lambda r: r["n"])
    report_path.write_text(json.dumps(data, indent=2) + "\n")


def test_gas_growth_properties(w3) -> None:
    """完整断言：线性随 n 近似线性增长，二分近似对数增长。"""
    sizes = [16, 128, 256]
    binary_gas = {}
    linear_gas = {}
    start = w3.eth.block_number + 5

    for n in sizes:
        c_binary = _deploy_named(w3, "Checkpoints")
        c_naive = _deploy_named(w3, "NaiveCheckpoints")
        # 交替部署后分别从相同高度建历史
        b_blocks = _build_history(w3, c_binary, n, start + n * 4)
        n_blocks = _build_history(w3, c_naive, n, start + n * 8)
        binary_gas[n] = c_binary.functions.valueAt(b_blocks[0]).estimate_gas()
        linear_gas[n] = c_naive.functions.valueAt(n_blocks[0]).estimate_gas()

    print("\n  n | binary | linear")
    for n in sizes:
        print(f"{n:4d} | {binary_gas[n]:6d} | {linear_gas[n]:6d}")

    # 线性扫描：256 比 16 显著增长（>6x；每次迭代约 2.4k gas）
    assert linear_gas[256] > linear_gas[16] * 6
    # 二分：256 仅比 16 多 ~4 次 warm 迭代 + 若干「首次冷读」检查点槽
    # （冷槽 ~2100 gas/槽，属一次性固定成本，不随查询重复发生）。
    # 给 15_000 上限；对比线性同区间增长（数十万 gas）。
    assert binary_gas[256] - binary_gas[16] < 15_000
    # 大历史下二分优势明显
    assert linear_gas[256] > binary_gas[256] * 10
