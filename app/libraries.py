"""链接库地址与库占位核验。

两种 solc 占位（均为 40 个 hex 字符，出现在半字节流中）：
- 新版 ``__$<keccak256(完全限定名) 前 34 hex>$__`` —— 可密码学核验绑定关系；
- 旧版 ``__<完全限定名，下划线补足/截断为 36 字符>__`` —— 名字可核验但易碰撞。
地址必须为 20 字节且通过 EIP-55 校验和。
"""

from __future__ import annotations

from . import crypto

PLACEHOLDER_LEN = 40  # nibbles == 20 bytes


class LibraryError(ValueError):
    """库地址或占位非法。"""


def parse_checksummed_address(address: str) -> bytes:
    """校验并返回地址的 20 字节原始值；必须是显式 EIP-55 校验和形式。"""
    if not isinstance(address, str):
        raise LibraryError("库地址必须是字符串")
    if not (address.startswith("0x") and len(address) == 42):
        raise LibraryError(f"库地址格式非法（需 0x+40 hex）: {address!r}")
    if not crypto.is_valid_eip55(address):
        raise LibraryError(f"库地址未通过 EIP-55 校验和: {address!r}")
    return bytes.fromhex(address[2:])


def fqn(source: str, lib_name: str) -> str:
    return f"{source}:{lib_name}"


def new_style_token(qualified: str) -> str:
    """新版占位原文：``__$`` + keccak256(FQN) 的前 34 hex + ``$__``。"""
    middle = crypto.keccak256(qualified.encode("utf-8")).hex()[:34]
    return f"__${middle}$__"


def legacy_token(qualified: str) -> str:
    """旧版占位原文：``__`` + FQN（超过 36 字符截断，不足下划线补齐）。"""
    name = qualified[:36]
    name = name + "_" * (36 - len(name))
    return f"__{name}__"


def is_new_style_token(token: str) -> bool:
    return len(token) == 40 and token.startswith("__$") and token.endswith("$__")


def expected_token_matches(qualified: str, token: str) -> tuple[bool, str | None]:
    """判断字节码中的占位与 FQN 的期望占位是否一致。

    返回 (是否一致, 实际风格 ``new``/``legacy``)。
    """
    if is_new_style_token(token):
        return token == new_style_token(qualified), "new"
    return token == legacy_token(qualified), "legacy"


def apply_linking(
    nibbles: str,
    regions: list[tuple[str, str, int, int]],
    libraries: dict[str, dict[str, str]],
) -> tuple[str, list[dict]]:
    """把配置中的库地址写入未链接字节码的占位位置，返回 (链接后hex, 核验证据)。

    每一步都核验：地址存在、EIP-55 合法、占位原文与 FQN 绑定一致、区间不重叠。
    """
    out = list(nibbles)
    evidence: list[dict] = []
    for src, lib, start, length in regions:
        qualified = fqn(src, lib)
        if length != PLACEHOLDER_LEN:
            raise LibraryError(f"{qualified} 占位长度 {length} 不是 40 nibble")
        token = "".join(out[start : start + length])
        matches, style = expected_token_matches(qualified, token)
        if not matches:
            raise LibraryError(
                f"字节码占位与库完全限定名不匹配: 期望 {new_style_token(qualified)}，"
                f"实际 {token}（{qualified}）"
            )
        libs_for_src = libraries.get(src)
        if not libs_for_src or lib not in libs_for_src:
            raise LibraryError(f"缺少库链接地址: {qualified}")
        addr_raw = parse_checksummed_address(libs_for_src[lib])
        out[start : start + length] = list(addr_raw.hex())
        evidence.append(
            {
                "library": qualified,
                "style": style,
                "start_nibble": start,
                "length_nibble": length,
                "address": crypto.to_eip55(addr_raw),
                "placeholder": token,
            }
        )
    # 链接完成后不得残留任何占位
    from .bytecode import find_placeholders

    leftovers = find_placeholders("".join(out))
    if leftovers:
        raise LibraryError(f"链接后仍有未解析占位: {leftovers}")
    return "".join(out), evidence


def validate_libraries_structure(libraries: object) -> dict[str, dict[str, str]]:
    """校验 settings.libraries 形如 {source: {LibName: address}}。"""
    if libraries is None:
        return {}
    if not isinstance(libraries, dict):
        raise LibraryError("libraries 必须是 {源路径: {库名: 地址}} 的对象")
    norm: dict[str, dict[str, str]] = {}
    for src, libs in libraries.items():
        if not isinstance(src, str) or not isinstance(libs, dict):
            raise LibraryError(f"libraries[{src!r}] 结构非法")
        norm[src] = {}
        for lib_name, addr in libs.items():
            if not isinstance(lib_name, str) or not isinstance(addr, str):
                raise LibraryError(f"库条目非法: {src}:{lib_name}")
            norm[src][lib_name] = addr
    return norm
