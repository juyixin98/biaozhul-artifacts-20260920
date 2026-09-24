"""链上交互：加载部署信息与 ABI、EIP-712 签名、交易封装。

仅连接本机 Anvil（HTTP），密钥全部来自 Anvil 公开测试密钥。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eth_account import Account
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "contracts" / "out"

OPERATION_FIELDS = [
    {"name": "target", "type": "address"},
    {"name": "data", "type": "bytes"},
    {"name": "value", "type": "uint256"},
    {"name": "nonce", "type": "uint256"},
    {"name": "validUntil", "type": "uint64"},
]


def load_abi(name: str) -> list:
    p = OUT_DIR / f"{name}.sol" / f"{name}.json"
    if not p.exists():
        raise FileNotFoundError(f"缺少编译产物 {p}，请先 forge build")
    return json.loads(p.read_text())["abi"]


@dataclass
class WalletService:
    """对 MultiSigTimelock 合约的封装。"""

    w3: Web3
    address: str
    abi: list
    counter_address: str
    threshold: int
    delay: int
    cooldown: int
    signer_keys: dict[str, bytes]  # checksum 地址(小写) -> 私钥
    next_nonce: int = 1

    @classmethod
    def from_deployment(cls, dep_path: str | Path) -> "WalletService":
        dep = json.loads(Path(dep_path).read_text())
        w3 = Web3(Web3.HTTPProvider(dep["rpc_url"], request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            raise ConnectionError(f"无法连接本地链 {dep['rpc_url']}")
        keys = {
            Web3.to_checksum_address(a).lower(): bytes.fromhex(k.removeprefix("0x"))
            for a, k in zip(dep["signers"], dep["signer_keys"])
        }
        return cls(
            w3=w3,
            address=Web3.to_checksum_address(dep["wallet"]),
            abi=load_abi("MultiSigTimelock"),
            counter_address=Web3.to_checksum_address(dep["counter"]),
            threshold=dep["threshold"],
            delay=dep["delay"],
            cooldown=dep["retry_cooldown"],
            signer_keys=keys,
        )

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------

    @property
    def contract(self):
        return self.w3.eth.contract(address=self.address, abi=self.abi)

    @property
    def signers(self) -> list[str]:
        raw = self.contract.functions.getSigners().call()
        return [self.w3.to_checksum_address(a) for a in raw]

    @property
    def chain_id(self) -> int:
        return self.w3.eth.chain_id

    def is_signer(self, addr: str) -> bool:
        return bool(self.contract.functions.isSigner(Web3.to_checksum_address(addr)).call())

    def get_operation(self, op_hash: bytes | str):
        row = self.contract.functions.getOperation(
            bytes.fromhex(str(op_hash).removeprefix("0x"))
        ).call()
        keys = ["target", "data", "value", "nonce", "validUntil", "scheduled",
                "executed", "readyAt", "lastFailureAt", "approvalCount"]
        return dict(zip(keys, row))

    def hash_operation(self, target: str, data: bytes, value: int,
                       nonce: int, valid_until: int) -> bytes:
        return bytes(self.contract.functions.hashOperation(
            Web3.to_checksum_address(target), data, value, nonce, valid_until
        ).call())

    # ------------------------------------------------------------------
    # EIP-712 离线签名
    # ------------------------------------------------------------------

    def sign_operation(self, signer_addr: str, target: str, data: bytes,
                       value: int, nonce: int, valid_until: int) -> bytes:
        addr = Web3.to_checksum_address(signer_addr).lower()
        if addr not in self.signer_keys:
            raise PermissionError(f"服务不持有签名人 {signer_addr} 的测试密钥")
        # eth-account >=0.13 的 EIP-712 API：types 不含 EIP712Domain
        domain = {
            "name": "MultiSigTimelock",
            "version": "1",
            "chainId": self.chain_id,
            "verifyingContract": Web3.to_checksum_address(self.address),
        }
        types = {"Operation": OPERATION_FIELDS}
        message = {
            "target": Web3.to_checksum_address(target),
            "data": data,
            "value": value,
            "nonce": nonce,
            "validUntil": valid_until,
        }
        sig = Account.sign_typed_data(self.signer_keys[addr], domain, types, message)
        # 65 字节 r(32) s(32) v(1)，与合约 _recover 的 65 字节分支一致
        return sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([sig.v])

    # ------------------------------------------------------------------
    # 交易
    # ------------------------------------------------------------------

    def send_function(self, from_addr: str, func) -> dict:
        """用本地测试密钥签名并发送一笔函数调用交易，返回收据 + 解码后的 revert。"""
        sender = Web3.to_checksum_address(from_addr)
        key = self.signer_keys.get(sender.lower())
        if key is None:
            # 非签名人（如 deployer）也允许发起只读入口交易；用第一个测试账户兜底
            # approve/execute 本身不做 msg.sender 权限限制，任何人都可提交。
            key = next(iter(self.signer_keys.values()))
        acct = Account.from_key(key)
        tx = func.build_transaction({
            "from": acct.address,
            "nonce": self.w3.eth.get_transaction_count(acct.address),
            "gas": 1_000_000,
            "gasPrice": self.w3.eth.gas_price,
            "chainId": self.chain_id,
        })
        signed = acct.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        return self._describe_receipt(receipt)

    def _describe_receipt(self, receipt) -> dict:
        status = receipt.get("status")
        info: dict[str, Any] = {
            "tx_hash": receipt["transactionHash"].hex(),
            "status": "success" if status == 1 else "reverted",
            "block_number": receipt["blockNumber"],
            "gas_used": receipt["gasUsed"],
        }
        if status != 1:
            info["error"] = self._decode_revert(receipt["transactionHash"])
        return info

    def _decode_revert(self, tx_hash) -> str:
        """重新模拟失败交易，把 revert 数据解码为 错误名(参数)。"""
        tx = self.w3.eth.get_transaction(tx_hash)
        try:
            self.w3.eth.call({
                "to": tx["to"], "data": tx["input"],
                "from": tx["from"], "value": tx["value"],
            }, tx["blockNumber"] - 1)
            return "execution reverted"
        except Exception as exc:
            raw = self._extract_revert_data(exc)
            if raw:
                decoded = self._decode_error_data(raw)
                if decoded:
                    return decoded
            return str(exc)

    @staticmethod
    def _extract_revert_data(exc) -> bytes | None:
        """从 web3 异常对象中尽力取出原始 revert 字节。"""
        data = getattr(exc, "data", None)
        candidates: list = []
        if isinstance(data, (bytes, str)):
            candidates.append(data)
        elif isinstance(data, (tuple, list)):
            candidates.extend(data)
        elif isinstance(data, dict):
            candidates.extend(data.values())
        # 兜底：从异常字符串里抓第一个 0x hex
        import re
        m = re.search(r"0x[0-9a-fA-F]{8,}", str(exc))
        if m:
            candidates.append(m.group(0))
        for c in candidates:
            try:
                if isinstance(c, str) and c.startswith("0x"):
                    return bytes.fromhex(c[2:])
                if isinstance(c, (bytes, bytearray)) and len(c) >= 4:
                    return bytes(c)
            except ValueError:
                continue
        return None

    def _decode_error_data(self, raw: bytes) -> str | None:
        """按 Error(string)/Panic/ABI 自定义错误解码 revert 数据。"""
        if len(raw) < 4:
            return None
        selector = raw[:4]
        # Error(string)
        if selector == bytes.fromhex("08c379a0") and len(raw) >= 68:
            try:
                from eth_abi import decode as abi_decode
                (msg,) = abi_decode(["string"], raw[4:])
                return f'Error("{msg}")'
            except Exception:
                pass
        # Panic(uint256)
        if selector == bytes.fromhex("4e487b71") and len(raw) >= 36:
            try:
                from eth_abi import decode as abi_decode
                (code,) = abi_decode(["uint256"], raw[4:])
                return f"Panic({code})"
            except Exception:
                pass
        # 自定义错误：从 ABI 建 selector -> (name, inputs)
        for item in self.abi:
            if item.get("type") != "error":
                continue
            sig = f'{item["name"]}({",".join(i["type"] for i in item.get("inputs", []))})'
            sel = self.w3.keccak(text=sig)[:4]
            if sel != selector:
                continue
            types = [i["type"] for i in item.get("inputs", [])]
            try:
                from eth_abi import decode as abi_decode
                values = abi_decode(types, raw[4:]) if types else []
                args = ",".join(self._fmt(v) for v in values)
            except Exception:
                args = raw[4:].hex()
            return f'{item["name"]}({args})'
        return f"0x{raw[:4].hex()}"

    @staticmethod
    def _fmt(v) -> str:
        if isinstance(v, (bytes, bytearray)):
            return "0x" + bytes(v).hex()
        return str(v)

    # ------------------------------------------------------------------
    # 时间（测试用）
    # ------------------------------------------------------------------

    def now(self) -> int:
        return self.w3.eth.get_block("latest")["timestamp"]

    def counter_value(self) -> int:
        abi = load_abi("Counter")
        return int(self.w3.eth.contract(address=self.counter_address, abi=abi)
                   .functions.count().call())

    def decode_logs(self, receipt) -> list[dict]:
        """把收据里的日志按 wallet ABI 解码为事件名 + 参数。"""
        contract = self.contract
        events: list[dict] = []
        for raw in receipt["logs"]:
            for ev in contract.events:
                # 只处理本钱包发出的日志
                if raw["address"].lower() != self.address.lower():
                    continue
                try:
                    parsed = ev().process_log(raw)
                except Exception:
                    continue
                args = {
                    k: ("0x" + v.hex() if isinstance(v, (bytes, bytearray)) else v)
                    for k, v in parsed["args"].items()
                }
                events.append({"event": parsed["event"], "args": args})
        return events
