"""单条链的 HTLC 读写封装（web3.py v6）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_utils import keccak
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from web3 import Web3
from web3.middleware import geth_poa_middleware
from web3.providers.rpc import HTTPProvider


def _no_retry_session() -> requests.Session:
    """urllib3 Retry(total=0) 的 session：挂起链按一次 timeout 快速失败。"""
    s = requests.Session()
    adapter = HTTPAdapter(max_retries=Retry(total=0, connect=0, read=0))
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


STATE_NAMES = ("Absent", "Locked", "Claimed", "Refunded")
ARTIFACT_PATH = Path("out/HTLC.sol/HTLC.json")


def load_abi(artifact: str | Path = ARTIFACT_PATH) -> list[dict[str, Any]]:
    return json.loads(Path(artifact).read_text())["abi"]


def _build_error_table(abi: list[dict[str, Any]]) -> dict[bytes, dict[str, Any]]:
    """构造自定义错误 selector -> {name, inputs} 映射。"""
    table: dict[bytes, dict[str, Any]] = {}
    for item in abi:
        if item.get("type") != "error":
            continue
        sig = f"{item['name']}({','.join(i['type'] for i in item['inputs'])})"
        table[keccak(text=sig)[:4]] = item
    return table


def decode_revert_data(data: str | bytes,
                       table: dict[bytes, dict[str, Any]] | None = None) -> str:
    """把合约回滚数据解码成文字（自定义错误 / Error(string) / 空 revert）。"""
    if isinstance(data, str):
        data = bytes.fromhex(data.removeprefix("0x"))
    if not data:
        return "无回滚数据（可能是 require/断言失败）"
    if data[:4] == b"\x08\xc3y\xa0":  # Panic(uint256)
        code = int.from_bytes(data[4 + 32 * 1:4 + 32 * 2], "big")
        return f"Panic(0x{code:02x})"
    if data[:4] == keccak(text="Error(string)")[:4]:
        try:
            from eth_abi import decode as abi_decode
            (msg,) = abi_decode(["string"], data[4:])
            return f"revert: {msg}"
        except Exception:
            return data.hex()
    if table and data[:4] in table:
        spec = table[data[:4]]
        try:
            from eth_abi import decode as abi_decode
            types = [i["type"] for i in spec["inputs"]]
            values = abi_decode(types, data[4:]) if types else ()
            rendered = ", ".join(
                f"{i['name']}={Web3.to_hex(v) if isinstance(v, (bytes, bytearray)) else v}"
                for i, v in zip(spec["inputs"], values)
            )
            return f"{spec['name']}({rendered})"
        except Exception:
            return spec["name"]
    return "0x" + data.hex()


def make_hash_lock(preimage: bytes) -> bytes:
    """与合约一致：keccak256(abi.encodePacked(bytes32))。"""
    return keccak(preimage)


def make_swap_id(seed: str) -> bytes:
    return keccak(text=seed)


@dataclass
class SwapView:
    leg: str
    rpc_url: str
    reachable: bool
    swap_id: str
    exists: bool = False
    sender: str | None = None
    receiver: str | None = None
    amount_wei: int | None = None
    hash_lock: str | None = None
    timelock: int | None = None
    state: str = "Unknown"  # Unknown（不可达）/ Absent / Locked / Claimed / Refunded
    preimage: str | None = None  # Claimed 时从事件中取回的原像
    now_ts: int | None = None
    seconds_until_timelock: int | None = None


class LegClient:
    def __init__(
        self,
        name: str,
        rpc_url: str,
        chain_id: int,
        address: str | None = None,
        deploy_block: int = 0,
        abi: list[dict[str, Any]] | None = None,
        request_timeout: float = 4.0,
    ) -> None:
        self.name = name
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.deploy_block = deploy_block
        self.w3 = Web3(
            HTTPProvider(
                rpc_url,
                request_kwargs={"timeout": request_timeout},
                session=_no_retry_session(),
            ),
        )
        # 注入 POA 兼容中间件（其余默认中间件保留）
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        # 移除 provider 层自带的 http_retry_request（它会让挂起链拖到 ~11s）
        try:
            self.w3.provider.middlewares = tuple(
                m for m in self.w3.provider.middlewares
                if getattr(m, "__name__", "") != "http_retry_request_middleware"
            )
        except AttributeError:
            pass
        self.address = Web3.to_checksum_address(address) if address else None
        self.contract = (
            self.w3.eth.contract(self.address, abi=abi or load_abi()) if self.address else None
        )
        self._error_selectors = _build_error_table(abi or load_abi())

    # ---------- 基础 ----------

    def reachable(self) -> bool:
        try:
            self.w3.eth.block_number
            return True
        except Exception:
            return False

    def now(self) -> int:
        return self.w3.eth.get_block("latest")["timestamp"]

    def account(self, private_key: str):
        return Account.from_key(private_key)

    def _tx_base(self, account) -> dict[str, Any]:
        return {
            "from": account.address,
            "nonce": self.w3.eth.get_transaction_count(account.address),
            "chainId": self.chain_id,
            "gas": 300_000,
            "gasPrice": self.w3.eth.gas_price,
        }

    def _send(self, account, func, value: int = 0) -> str:
        """构造、签名、发送交易，等待收据并把 revert 原因解码为可读异常。"""
        if self.contract is None:
            raise RuntimeError(f"{self.name}: 合约地址未配置（先运行 scripts/deploy.py）")
        tx = func.build_transaction({**self._tx_base(account), "value": value})
        signed = account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction).hex()
        rcpt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=20)
        if rcpt.status != 1:
            reason = self._revert_reason(tx)
            raise RuntimeError(f"{self.name}: 交易回滚 {tx_hash} {reason}")
        return tx_hash

    def _revert_reason(self, tx: dict[str, Any]) -> str:
        """用 eth_call 在最新区块重放，捕获回滚数据并按自定义错误解码。"""
        try:
            self.w3.eth.call(
                {
                    "to": tx["to"],
                    "data": tx["data"],
                    "from": tx["from"],
                    "value": tx["value"],
                },
                self.w3.eth.block_number,
            )
            return ""
        except Exception as exc:  # noqa: BLE001 - 仅用于呈现错误信息
            # web3 v6：自定义错误抛 ContractCustomError，data 为完整回滚字节
            data = getattr(exc, "data", None)
            if isinstance(data, (str, bytes)) and len(data) >= 4:
                return decode_revert_data(data, self._error_selectors)
            return type(exc).__name__

    # ---------- 写操作（测试/演示脚本使用，协调器不暴露写接口） ----------

    def lock(self, private_key: str, swap_id: bytes, receiver: str,
             hash_lock: bytes, timelock: int, amount_wei: int) -> str:
        acct = self.account(private_key)
        func = self.contract.functions.lock(
            swap_id, Web3.to_checksum_address(receiver), hash_lock, timelock
        )
        return self._send(acct, func, value=amount_wei)

    def claim(self, private_key: str, swap_id: bytes, preimage: bytes) -> str:
        acct = self.account(private_key)
        return self._send(acct, self.contract.functions.claim(swap_id, preimage))

    def refund(self, private_key: str, swap_id: bytes) -> str:
        acct = self.account(private_key)
        return self._send(acct, self.contract.functions.refund(swap_id))

    # ---------- 读操作 ----------

    def get_swap(self, swap_id: bytes) -> SwapView:
        view = SwapView(leg=self.name, rpc_url=self.rpc_url, reachable=False,
                        swap_id=swap_id.hex())
        try:
            view.reachable = True
            view.now_ts = self.now()
            sender, receiver, amount, hlock, timelock, state_int = (
                self.contract.functions.getSwap(swap_id).call()
            )
            view.exists = state_int != 0 or hlock != b"\x00" * 32
            view.sender = sender
            view.receiver = receiver
            view.amount_wei = amount
            view.hash_lock = hlock.hex()
            view.timelock = timelock
            view.state = STATE_NAMES[state_int]
            view.seconds_until_timelock = timelock - view.now_ts
            if state_int == 2:  # Claimed：从事件中取原像
                view.preimage = self._claimed_preimage(swap_id)
        except Exception:
            view.reachable = False
            view.state = "Unknown"
        return view

    def _claimed_preimage(self, swap_id: bytes) -> str | None:
        try:
            logs = self.contract.events.Claimed().get_logs(
                fromBlock=self.deploy_block, argument_filters={"id": swap_id}
            )
            if logs:
                return logs[-1]["args"]["preimage"].hex()
        except Exception:
            pass
        return None

    def list_locked_ids(self) -> list[str]:
        try:
            logs = self.contract.events.Locked().get_logs(fromBlock=self.deploy_block)
            return [log["args"]["id"].hex() for log in logs]
        except Exception:
            return []

    def balance(self) -> int | None:
        try:
            return self.w3.eth.get_balance(self.address)
        except Exception:
            return None

    # ---------- 仅部署脚本使用 ----------

    @staticmethod
    def deploy(rpc_url: str, chain_id: int, deployer_key: str,
               artifact: str | Path = ARTIFACT_PATH) -> tuple[str, int]:
        w3 = Web3(HTTPProvider(rpc_url, session=_no_retry_session()))
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        art = json.loads(Path(artifact).read_text())
        acct = Account.from_key(deployer_key)
        tx = {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": chain_id,
            "gas": 3_000_000,
            "gasPrice": w3.eth.gas_price,
            "data": art["bytecode"]["object"],
        }
        signed = acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction).hex()
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        if rcpt.status != 1:
            raise RuntimeError(f"部署失败 @ {rpc_url}: {tx_hash}")
        return rcpt.contractAddress, rcpt.blockNumber


def packed_preimage(raw: str) -> bytes:
    """把 0x 开头的 32 字节 hex 变成合约期望的 bytes32 原像。"""
    b = bytes.fromhex(raw.removeprefix("0x"))
    if len(b) != 32:
        raise ValueError("preimage 必须是 32 字节")
    return b
