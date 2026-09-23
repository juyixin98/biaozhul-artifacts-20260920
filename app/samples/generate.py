"""确定性生成「solc 标准 JSON 输出风格」的示例编译产物。

这些样例不是用 solc 编译的，但全部密码学结构都是真实构造的：
- metadata JSON 中 keccak256 源摘要为真实 keccak-256；
- 字节码末尾 CBOR 段为真实 CBOR 编码（cbor2 canonical），内嵌真实
  SHA-256(metadata JSON)，与 solc 行为一致，可被服务端独立重算验证；
- 库占位为真实的 ``__$keccak256(FQN)[:34]$__`` 形式，链接地址为 EIP-55。

用法::

    python -m app.samples.generate --out examples [--variant all]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import tarfile
import zipfile
from dataclasses import dataclass

import cbor2

from .. import crypto
from ..libraries import new_style_token
from ..service import canonical_json

DEFAULT_LIB_SRC = "src/SafeMath.sol"
DEFAULT_LIB_NAME = "SafeMath"
DEFAULT_SRC = "src/Vault.sol"
DEFAULT_CONTRACT = "Vault"
DEFAULT_VERSION = "0.8.24+commit.e11b9ed9"
DEFAULT_ADDR = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"  # EIP-55 官方向量

PLACEHOLDER_POS = 200  # nibbles
BODY_NIBBLES = 480    # body 总长度（不含 CBOR 尾）


def deterministic_body(seed: str, length: int = BODY_NIBBLES) -> str:
    """由种子确定性派生半字节流；模拟编译器产生的 runtime code。"""
    out = ""
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(f"{seed}:{counter}".encode()).hexdigest()
        counter += 1
    return out[:length]


def make_metadata(
    *,
    sources: dict[str, bytes],
    source_entries: dict[str, str],
    version: str,
    optimize: bool,
    runs: int,
    via_ir: bool,
    evm_version: str,
    libraries: dict[str, dict[str, str]],
) -> dict:
    src_section = {}
    for path in source_entries:
        src_section[path] = {"keccak256": "0x" + crypto.keccak256(sources[path]).hex()}
    lib_section = {s: {n: a for n, a in libs.items()} for s, libs in libraries.items() if libs}
    settings = {
        "evmVersion": evm_version,
        "optimizer": {"enabled": optimize, "runs": runs},
        "outputSelection": {"*": {"*": ["abi", "evm.bytecode", "evm.deployedBytecode", "metadata"]}},
    }
    if via_ir:
        settings["viaIR"] = True
    if lib_section:
        settings["libraries"] = lib_section
    return {
        "compiler": {"version": version},
        "language": "Solidity",
        "outputSelection": settings["outputSelection"],
        "settings": settings,
        "sources": src_section,
        "version": 1,
    }


def build_bytecode(
    *,
    body_seed: str,
    metadata_raw: bytes,
    version: str,
    with_placeholder: bool,
    lib_src: str,
    lib_name: str,
    cbor_solc_triple: tuple[int, int, int] | None = None,
) -> tuple[str, list[tuple[str, str, int, int]]]:
    """构造未链接字节码 hex + linkReferences。CBOR 尾真实编码。"""
    body = deterministic_body(body_seed)
    refs: list[tuple[str, str, int, int]] = []
    if with_placeholder:
        token = new_style_token(f"{lib_src}:{lib_name}")
        body = body[:PLACEHOLDER_POS] + token + body[PLACEHOLDER_POS + 40 :]
        refs.append((lib_src, lib_name, PLACEHOLDER_POS, 40))
    triple = cbor_solc_triple or tuple(int(x) for x in version.split("+")[0].split("."))
    cbor_map = {"ipfs": crypto.sha256(metadata_raw), "solc": bytes(triple)}
    cbor_seg = cbor2.dumps(cbor_map, canonical=True)
    tail = cbor_seg + len(cbor_seg).to_bytes(2, "big")
    return body + tail.hex(), refs


def link_bytecode(nibbles: str, refs: list[tuple[str, str, int, int]], address: str) -> str:
    addr_hex = address[2:].lower()
    out = list(nibbles)
    for _, _, start, length in refs:
        out[start : start + length] = list(addr_hex)
    return "".join(out)


def make_sources(kind: str = "main", semantic_seed: str = "v1") -> dict[str, bytes]:
    main = f"""// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import "./SafeMath.sol";

/// @dev 溯源示例主合约（语义种子 {semantic_seed}，kind={kind}）
contract {DEFAULT_CONTRACT} {{
    using SafeMath for uint256;
    uint256 public total;
    address public owner;
    constructor() {{ owner = msg.sender; }}
    function deposit(uint256 v) external {{
        total = total.add(v);
        if (uint160(msg.sender) % 2 == 0) {{ total += 1; }}  // 语义种子 {semantic_seed}
    }}
}}
"""
    lib = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

library SafeMath {
    function add(uint256 a, uint256 b) internal pure returns (uint256) {
        uint256 c = a + b;
        require(c >= a, "SafeMath: addition overflow");
        return c;
    }
}
"""
    return {DEFAULT_SRC: main.encode(), DEFAULT_LIB_SRC: lib.encode()}


@dataclass
class Variant:
    key: str
    description: str
    expected_verdict: str
    use_libs: bool = False
    optimize: bool = True
    runs: int = 200
    evm_version: str = "cancun"
    via_ir: bool = False
    version: str = DEFAULT_VERSION
    target_alt: str | None = None  # metadata | libaddr | semantic | optimizer | version
    target_alt_address: str | None = None
    drop_source: str | None = None
    cbor_solc_triple: tuple[int, int, int] | None = None


# 第二个 EIP-55 有效地址（EIP-55 向量）
ALT_ADDR = "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359"


VARIANTS = [
    Variant("01-exact", "无库、标准优化，部署字节码完全一致", "EXACT"),
    Variant(
        "02-rule-libaddr",
        "含链接库，仅库地址不同（规则内匹配）",
        "RULE_MATCH",
        use_libs=True,
        target_alt="libaddr",
        target_alt_address=ALT_ADDR,
    ),
    Variant(
        "03-rule-metadata",
        "仅 CBOR 元数据尾不同（规则内匹配，代码语义一致）",
        "RULE_MATCH",
        target_alt="metadata",
    ),
    Variant(
        "04-mismatch-optimizer",
        "配置声称 runs=200，产物实际按 runs=999999 生成（规范化不得掩盖）",
        "MISMATCH",
        runs=999999,
        optimize=True,
    ),
    Variant(
        "05-missing-source",
        "源码包缺失被编译文件（缺源码必须失败）",
        "MISMATCH",
        drop_source=DEFAULT_SRC,
    ),
    Variant(
        "06-mismatch-semantic",
        "字节码主体被改动（无法解释的差异，必须失败）",
        "MISMATCH",
        target_alt="semantic",
    ),
    Variant(
        "07-mismatch-version",
        "配置版本 0.8.24 与产物记录版本 0.8.20 不一致",
        "MISMATCH",
        version="0.8.20+commit.a1b2c3d4",
    ),
    Variant(
        "08-bad-library-address",
        "配置库地址未通过 EIP-55 校验（请求直接 400 拒绝）",
        "HTTP_400",
        use_libs=True,
    ),
]


def build_variant(v: Variant, out_dir: str) -> None:
    sources = make_sources(semantic_seed=v.key)
    source_entries = [DEFAULT_SRC] + ([DEFAULT_LIB_SRC] if v.use_libs else [])
    libraries: dict[str, dict[str, str]] = {}
    if v.use_libs:
        # 08 故意给出非校验和地址；其余样例给 EIP-55 有效地址。
        addr = "0x5AAeB6053F3E94C9B9A09F33669435E7EF1BEAE" if v.key.startswith("08") else DEFAULT_ADDR
        libraries = {DEFAULT_LIB_SRC: {DEFAULT_LIB_NAME: addr}}

    # 元数据模拟「部署期链接」工作流：编译产物未固化库地址，settings 不写 libraries。
    metadata = make_metadata(
        sources=sources,
        source_entries={p: sources[p] for p in source_entries},
        version=v.version,
        optimize=v.optimize,
        runs=v.runs,
        via_ir=v.via_ir,
        evm_version=v.evm_version,
        libraries={},
    )
    metadata_raw = canonical_json(metadata)

    body_seed = f"{v.key}:{v.runs}:{v.evm_version}:{v.via_ir}"
    deployed_unlinked, dep_refs = build_bytecode(
        body_seed=body_seed,
        metadata_raw=metadata_raw,
        version=v.version,
        with_placeholder=v.use_libs,
        lib_src=DEFAULT_LIB_SRC,
        lib_name=DEFAULT_LIB_NAME,
        cbor_solc_triple=v.cbor_solc_triple,
    )
    creation_unlinked, cre_refs = build_bytecode(
        body_seed="creation:" + body_seed,
        metadata_raw=metadata_raw,
        version=v.version,
        with_placeholder=v.use_libs,
        lib_src=DEFAULT_LIB_SRC,
        lib_name=DEFAULT_LIB_NAME,
        cbor_solc_triple=v.cbor_solc_triple,
    )

    def evm_obj(nibbles: str, refs: list) -> dict:
        link_refs: dict[str, dict[str, list]] = {}
        for src, lib, start, length in refs:
            link_refs.setdefault(src, {}).setdefault(lib, []).append({"start": start, "length": length})
        return {"object": "0x" + nibbles, "opcodes": "", "sourceMap": "", "linkReferences": link_refs}

    compiler_output = {
        "contracts": {
            DEFAULT_SRC: {
                DEFAULT_CONTRACT: {
                    "abi": [
                        {"type": "function", "name": "deposit", "inputs": [{"name": "v", "type": "uint256"}],
                         "outputs": [], "stateMutability": "nonpayable"},
                        {"type": "constructor", "inputs": [], "stateMutability": "nonpayable"},
                    ],
                    "metadata": metadata_raw.decode("utf-8"),
                    "evm": {
                        "bytecode": evm_obj(creation_unlinked, cre_refs),
                        "deployedBytecode": evm_obj(deployed_unlinked, dep_refs),
                    },
                }
            }
        },
        "version": v.version,
    }

    # 构造目标部署字节码：
    # - 02：期望按配置地址 DEFAULT 链接，但链上目标实际链接到另一个合法地址 ALT
    # - 08：配置地址本身非法（在服务入口即被 400 拒绝），目标仍给合法链接结果
    link_addr_for_target = v.target_alt_address if v.target_alt == "libaddr" else DEFAULT_ADDR
    target = link_bytecode(deployed_unlinked, dep_refs, link_addr_for_target)

    if v.target_alt == "metadata":
        alt_meta_raw = metadata_raw + b" "  # 仅空白变化 -> metadata JSON 语义不变但哈希变
        seg = cbor2.dumps(
            {"ipfs": crypto.sha256(alt_meta_raw), "solc": bytes(
                int(x) for x in v.version.split("+")[0].split("."))},
            canonical=True,
        )
        new_tail = (seg + len(seg).to_bytes(2, "big")).hex()
        # CBOR 段定长（同键同结构），等量替换最末尾
        old_tail_len = int(target[-4:], 16) * 2 + 4
        target = target[: len(target) - old_tail_len] + new_tail
    elif v.target_alt == "semantic":
        # 在代码主体（非占位、非元数据尾）翻转两个 nibble
        t = list(target)
        t[10] = f"{(int(t[10], 16) ^ 0xF):x}"
        t[11] = f"{(int(t[11], 16) ^ 0x5):x}"
        target = "".join(t)
    elif v.target_alt == "optimizer":
        other = deterministic_body(f"{v.key}:1:{v.evm_version}:{v.via_ir}")
        target = other + target[-(len(target) - BODY_NIBBLES):]

    # config 中声明的编译版本；07 故意与产物记录的 v.version 不一致
    if v.key.startswith("07"):
        cfg_version = DEFAULT_VERSION
    else:
        cfg_version = v.version
    config = {
        "compilerVersion": cfg_version,
        "source": DEFAULT_SRC,
        "name": DEFAULT_CONTRACT,
        "optimizer": {"enabled": v.optimize, "runs": 200 if v.key.startswith("04") else v.runs},
        "viaIR": v.via_ir,
        "evmVersion": v.evm_version,
        "libraries": libraries,
    }
    if not libraries:
        del config["libraries"]

    vdir = os.path.join(out_dir, v.key)
    os.makedirs(vdir, exist_ok=True)
    with open(os.path.join(vdir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(vdir, "compilerOutput.json"), "w", encoding="utf-8") as fh:
        json.dump(compiler_output, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(vdir, "target_deployed.hex"), "w", encoding="ascii") as fh:
        fh.write("0x" + target)
    pkg_sources = {p: data for p, data in sources.items() if p != v.drop_source}
    _write_zip(os.path.join(vdir, "sources.zip"), pkg_sources)
    _write_targz(os.path.join(vdir, "sources.tar.gz"), pkg_sources)
    readme = {
        "description": v.description,
        "expected_verdict": v.expected_verdict,
        "files": ["config.json", "compilerOutput.json", "target_deployed.hex", "sources.zip", "sources.tar.gz"],
    }
    if v.drop_source:
        readme["dropped_from_package"] = v.drop_source
    with open(os.path.join(vdir, "README.json"), "w", encoding="utf-8") as fh:
        json.dump(readme, fh, ensure_ascii=False, indent=2)


def _write_zip(path: str, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in sorted(files.items()):
            zf.writestr(name, data)


def _write_targz(path: str, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _write_nul_zip(path: str, data: bytes) -> None:
    """手工构造文件名含真实 NUL 字节的 ZIP（zipfile.writestr 会截断 NUL）。"""
    import struct
    import zlib

    name = b"src/A\x00.sol"
    flag = 0x0800  # UTF-8 名称
    comp = zlib.compressobj(6, zlib.DEFLATED, -15)
    cdata = comp.compress(data) + comp.flush()
    crc = zlib.crc32(data) & 0xFFFFFFFF
    local = (
        b"PK\x03\x04"
        + struct.pack("<HHHHHIIIHH", 20, flag, 8, 0, 0, crc, len(cdata), len(data), len(name), 0)
        + name
        + cdata
    )
    central = (
        b"PK\x01\x02"
        + struct.pack(
            "<HHHHHHIIIHHHHHII",
            0x0314, 20, flag, 8, 0, 0, 0, crc, len(cdata), len(data),
            len(name), 0, 0, 0, 0, 0,
        )
        + name
    )
    eocd = b"PK\x05\x06" + struct.pack("<HHHHIIH", 0, 0, 1, 1, len(central), len(local), 0)
    with open(path, "wb") as fh:
        fh.write(local + central + eocd)


def build_malicious_packages(out_dir: str) -> None:
    pkg_dir = os.path.join(out_dir, "_malicious")
    os.makedirs(pkg_dir, exist_ok=True)
    evil = b"// not a real contract\n"
    # 1) zip slip
    with zipfile.ZipFile(os.path.join(pkg_dir, "zipslip.zip"), "w") as zf:
        zf.writestr("../escape.sol", evil)
        zf.writestr("src/Vault.sol", b"pragma solidity ^0.8.24;")
    # 2) 绝对路径
    with zipfile.ZipFile(os.path.join(pkg_dir, "absolute.zip"), "w") as zf:
        zf.writestr("/tmp/escape.sol", evil)
    # 3) 反斜杠（Windows 路径穿越变体）
    with zipfile.ZipFile(os.path.join(pkg_dir, "backslash.zip"), "w") as zf:
        zf.writestr("..\\..\\escape.sol", evil)
    # 4) tar 符号链接
    with tarfile.open(os.path.join(pkg_dir, "symlink.tar"), "w") as tf:
        info = tarfile.TarInfo("src/Vault.sol")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../../../etc/passwd"
        tf.addfile(info)
    # 5) 重复/冲突
    with zipfile.ZipFile(os.path.join(pkg_dir, "duplicate.zip"), "w") as zf:
        zf.writestr("src/A.sol", b"a")
        zf.writestr("src//A.sol", b"b")  # 规范化后冲突
    # 6) NUL 字符（手工构造 ZIP 本地头+中央目录，绕过 writestr 的 NUL 截断）
    _write_nul_zip(os.path.join(pkg_dir, "nul.zip"), b"// evil\n")
    # 7) tar 硬链接
    with tarfile.open(os.path.join(pkg_dir, "hardlink.tar"), "w") as tf:
        data = b"x"
        good = tarfile.TarInfo("src/good.sol")
        good.size = 1
        tf.addfile(good, io.BytesIO(data))
        link = tarfile.TarInfo("src/link.sol")
        link.type = tarfile.LNKTYPE
        link.linkname = "src/good.sol"
        tf.addfile(link)
    with open(os.path.join(pkg_dir, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "# 恶意/畸形归档样例\n\n"
            "每个文件都应被解包器以 400 BAD_PACKAGE 拒绝：\n"
            "- zipslip.zip：`../` 路径穿越\n"
            "- absolute.zip：绝对路径\n"
            "- backslash.zip：反斜杠路径歧义\n"
            "- symlink.tar：符号链接\n"
            "- hardlink.tar：硬链接\n"
            "- duplicate.zip：规范化后条目冲突\n"
            "- nul.zip：NUL 控制字符\n"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="examples")
    parser.add_argument("--variant", default="all")
    args = parser.parse_args()
    keys = {v.key for v in VARIANTS}
    chosen = VARIANTS if args.variant == "all" else [v for v in VARIANTS if v.key == args.variant]
    if not chosen:
        raise SystemExit(f"未知 variant: {args.variant}，可选: {sorted(keys)} / all")
    for v in chosen:
        build_variant(v, args.out)
        print(f"built {v.key} -> expected {v.expected_verdict}")
    build_malicious_packages(args.out)
    print("built malicious packages -> examples/_malicious")


if __name__ == "__main__":
    main()
