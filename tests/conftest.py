"""pytest 固件：自动启动本机 Anvil、部署合约、构造 FastAPI 测试客户端。

不依赖外部已运行的节点——每个测试会话启动独立 Anvil（端口 0 = 系统分配）。
需要 forge（构建）和 anvil 在 PATH 或 ~/.foundry/bin 中。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient
from web3 import Web3
from web3 import HTTPProvider

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.chain import CheckpointClient  # noqa: E402
from app.config import DEFAULT_ANVIL_PRIVATE_KEY  # noqa: E402
from app.main import create_app  # noqa: E402


def _foundry_bin(name: str) -> str:
    """在 PATH 与 ~/.foundry/bin 中查找 forge/anvil。"""
    found = shutil.which(name)
    if found:
        return found
    candidate = pathlib.Path.home() / ".foundry" / "bin" / name
    if candidate.exists():
        return str(candidate)
    raise RuntimeError(f"找不到 {name}，请安装 Foundry")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_rpc(w3: Web3, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if w3.is_connected():
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("Anvil 在超时时间内未就绪")


def _build() -> None:
    forge = _foundry_bin("forge")
    subprocess.run([forge, "build"], cwd=ROOT, check=True, capture_output=True)


def _deploy(w3: Web3) -> str:
    artifact = json.loads(
        (ROOT / "out" / "Checkpoints.sol" / "Checkpoints.json").read_text()
    )
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
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    return receipt["contractAddress"]


@pytest.fixture(scope="session")
def anvil():
    _build()
    port = _free_port()
    anvil_bin = _foundry_bin("anvil")
    proc = subprocess.Popen(
        [
            anvil_bin,
            "--port", str(port),
            "--silent",
            "--gas-limit", "30000000",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    rpc_url = f"http://127.0.0.1:{port}"
    w3 = Web3(HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
    try:
        _wait_rpc(w3)
        # 先产生一个块再拍快照：在创世块（0）上 revert 会让 Anvil 异常。
        w3.provider.make_request("evm_mine", [])
        snap_resp = w3.provider.make_request("evm_snapshot", [])
        state = {
            "w3": w3,
            "rpc_url": rpc_url,
            "proc": proc,
            # 快照 ID 可能是 "0x0"，不能用真值判断
            "snapshot_id": snap_resp["result"],
        }
        yield state
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def fresh_chain(anvil):
    """每个测试回滚到全新链状态（区块归零、nonce 归零、交易池清空）。

    Anvil 的 evm_snapshot 是一次性的（revert 后该快照失效），
    因此用全局「当前快照」：setup 时回滚到它、teardown 时回滚后再重拍。
    """
    w3 = anvil["w3"]
    # 进入时 anvil 已回滚到「上一个测试结束后重拍」的干净快照。
    # 注意：不能调用 evm_setIntervalMining(0) —— 该 Anvil 版本会因此
    # 关闭自动出块；automine 状态随快照回滚恢复即可。
    w3.provider.make_request("evm_setAutomine", [True])
    snapshot_id = anvil["snapshot_id"]
    yield w3
    # 回滚当前快照（挖矿模式随之恢复），并为下一个测试重拍新快照
    w3.provider.make_request("evm_setAutomine", [True])
    w3.provider.make_request("evm_revert", [snapshot_id])
    anvil["snapshot_id"] = w3.provider.make_request("evm_snapshot", [])["result"]


@pytest.fixture()
def chain_client(fresh_chain) -> CheckpointClient:
    """每个测试一个全新部署的合约，链状态也已回滚到创世。"""
    w3 = fresh_chain
    address = _deploy(w3)
    artifact = json.loads(
        (ROOT / "out" / "Checkpoints.sol" / "Checkpoints.json").read_text()
    )
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(address), abi=artifact["abi"]
    )
    return CheckpointClient(w3, contract, DEFAULT_ANVIL_PRIVATE_KEY)


@pytest.fixture()
def client(chain_client) -> TestClient:
    app = create_app(client=chain_client)
    return TestClient(app)


@pytest.fixture()
def w3(fresh_chain):
    return fresh_chain


class ChainControl:
    """对 Anvil 的挖矿控制，用于构造同块/跨块/空隙场景。

    注意 Anvil 语义：创世块为 0，automine 开启时发送交易会产出
    「当前最新块 + 1」号块。因此「让下一笔交易进入第 B 块」需要
    先把链推进到 B-1（mine_to(B-1)）。
    """

    def __init__(self, w3: Web3) -> None:
        self.w3 = w3

    def mine(self, n: int = 1) -> None:
        """原地挖 n 个空块。"""
        if n > 0:
            self.w3.provider.make_request("evm_mine", [{"blocks": n}])

    def mine_to(self, block: int) -> None:
        """推进到指定最新块号（下一笔交易将进入 block+1）。"""
        gap = block - self.w3.eth.block_number
        if gap > 0:
            self.mine(gap)

    def set_automine(self, enabled: bool) -> None:
        self.w3.provider.make_request("evm_setAutomine", [enabled])

    def mine_one(self) -> None:
        self.w3.provider.make_request("evm_mine", [])

    def batch_mine_pending(self) -> int:
        """automine 关闭时：把交易池里的 pending 交易统一打进一个新块。

        不再重新开启 automine（重开瞬间 Anvil 可能立即再挖一块）；
        测试结束后由 fresh_chain 的 evm_revert 恢复 automine 与链状态。
        返回打包后的当前块号。
        """
        self.w3.provider.make_request("evm_mine", [])
        return self.w3.eth.block_number

    @property
    def block(self) -> int:
        return self.w3.eth.block_number


@pytest.fixture()
def chain(w3) -> ChainControl:
    return ChainControl(w3)


def fire_set_value(chain_client: CheckpointClient, value: int) -> None:
    """发送 setValue 但不等回执（用于同块打包）。

    gasPrice 相对网络基础价随 nonce 微增，避免 Anvil 把同 nonce
    池内交易误判为 replacement-underpriced。
    """
    w3 = chain_client.w3
    nonce = w3.eth.get_transaction_count(
        chain_client.acct.address, "pending"
    )
    tx = chain_client.contract.functions.setValue(value).build_transaction(
        {
            "from": chain_client.acct.address,
            "nonce": nonce,
            "gas": 300_000,
            "gasPrice": w3.eth.gas_price + nonce,
            "chainId": w3.eth.chain_id,
        }
    )
    signed = chain_client.acct.sign_transaction(tx)
    w3.eth.send_raw_transaction(signed.raw_transaction)
