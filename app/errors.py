"""把合约自定义错误的 revert data 解码成可读文本。"""
from __future__ import annotations

from eth_abi import decode as abi_decode
from web3 import Web3

# 自定义错误签名（与 BoundedSettlement.sol 保持一致）
_ERRORS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ZeroAddress()": ("ZeroAddress", ()),
    "SameToken()": ("SameToken", ()),
    "ZeroAmount()": ("ZeroAmount", ()),
    "OrderExpired(uint256,uint256)": ("OrderExpired", ("uint256", "uint256")),
    "NonceAlreadyCancelled(address,uint256)": (
        "NonceAlreadyCancelled",
        ("address", "uint256"),
    ),
    "InvalidSignature(address,address)": ("InvalidSignature", ("address", "address")),
    "BadSignatureLength(uint256)": ("BadSignatureLength", ("uint256",)),
    "BadSignatureV(uint8)": ("BadSignatureV", ("uint8",)),
    "BadSignatureS()": ("BadSignatureS", ()),
    "FillExceedsRemaining(uint256,uint256)": (
        "FillExceedsRemaining",
        ("uint256", "uint256"),
    ),
    "FeeCapExceeded(uint256,uint256)": ("FeeCapExceeded", ("uint256", "uint256")),
    "TransferFailed(address,address,address,uint256)": (
        "TransferFailed",
        ("address", "address", "address", "uint256"),
    ),
    "NotOwner()": ("NotOwner", ()),
}

SELECTORS: dict[bytes, tuple[str, tuple[str, ...]]] = {}
for _sig, _def in _ERRORS.items():
    SELECTORS[bytes(Web3.keccak(text=_sig)[:4])] = _def


def extract_revert_data(exc: BaseException) -> bytes | None:
    """尽力从 web3 异常中取出原始 revert data。"""
    candidates: list[object] = [getattr(exc, "data", None), getattr(exc, "message", None), str(exc)]
    for cand in candidates:
        data = _dig_hex(cand)
        if data is not None:
            return data
    return None


def _dig_hex(obj: object) -> bytes | None:
    if isinstance(obj, str):
        s = obj.strip()
        # 形如 Revert 0x.... / contract runner revert reason: 0x....
        if "0x" in s:
            hexpart = s[s.rfind("0x"):]
            try:
                return bytes.fromhex(hexpart[2:])
            except ValueError:
                return None
        return None
    if isinstance(obj, bytes):
        return obj
    if isinstance(obj, dict):
        # web3 某些版本: {"originalError": {"data": "0x..."}}
        for v in obj.values():
            found = _dig_hex(v)
            if found is not None:
                return found
    return None


def decode_revert(data: bytes | None) -> str | None:
    if not data or len(data) < 4:
        return None
    sel = bytes(data[:4])
    item = SELECTORS.get(sel)
    if item is None:
        return f"0x{data.hex()}"
    name, types = item
    if not types:
        return name
    try:
        values = abi_decode(types, data[4:])
    except Exception:  # noqa: BLE001 - 解码失败时退回原始 hex
        return f"{name}(0x{data.hex()})"
    pretty = ", ".join(str(v) for v in values)
    return f"{name}({pretty})"
