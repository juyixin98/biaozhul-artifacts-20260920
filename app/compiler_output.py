"""解析并校验「已生成的编译器输出」（solc standard-json 输出）与编译配置。

本服务**不调用编译器**，只核验调用者提交的既有产物，因此对结构完整性
零容忍：字段缺失/类型错误一律拒绝，而不是猜测默认值。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?(\+[0-9A-Za-z.\-]+)?$")


class CompilerOutputError(ValueError):
    """compilerOutput 结构非法。"""


@dataclass
class ContractArtifact:
    source: str
    name: str
    metadata_raw: bytes
    metadata: dict
    deployed_bytecode: str          # 未链接 nibble hex
    deployed_link_refs: list        # [(src, lib, start, length)]
    creation_bytecode: str          # 未链接 nibble hex
    creation_link_refs: list
    abi: object = None


@dataclass
class CompilationConfig:
    compiler_version: str
    source: str
    name: str
    evm_version: str | None
    optimize: bool
    optimize_runs: int | None
    via_ir: bool
    libraries: dict
    extra_settings: dict = field(default_factory=dict)

    def canonical_settings(self) -> dict:
        """用于与 metadata.settings 逐项对比的规范化视图（不做语义猜测）。

        缺失的可选项保留 None/缺省，确保"未声明"与"显式关闭"不会被混为一谈。
        """
        return {
            "compiler_version": self.compiler_version,
            "evm_version": self.evm_version,
            "optimize": self.optimize,
            "optimize_runs": self.optimize_runs,
            "via_ir": self.via_ir,
            "libraries": self.libraries,
        }


def parse_version(text: str) -> tuple[int, int, int]:
    """提取 ``major.minor.patch``；接受 solc 长版本但拒绝完全无法识别的串。"""
    if not isinstance(text, str):
        raise CompilerOutputError(f"编译器版本不是字符串: {text!r}")
    s = text.strip()
    if s.startswith("v"):
        s = s[1:]
    m = _VERSION_RE.match(s)
    if not m:
        raise CompilerOutputError(f"无法解析的编译器版本: {text!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _require(obj: dict, key: str, ctx: str, typ: type | tuple):
    if key not in obj:
        raise CompilerOutputError(f"{ctx} 缺少字段: {key}")
    if not isinstance(obj[key], typ):
        raise CompilerOutputError(f"{ctx}.{key} 类型错误: 期望 {typ}")
    return obj[key]


def parse_config(cfg: dict) -> CompilationConfig:
    """解析请求中的编译配置。"""
    if not isinstance(cfg, dict):
        raise CompilerOutputError("config 必须是 JSON 对象")
    version = _require(cfg, "compilerVersion", "config", str)
    parse_version(version)  # 提前拒绝非法版本
    source = _require(cfg, "source", "config", str)
    name = _require(cfg, "name", "config", str)
    optimizer = cfg.get("optimizer", {})
    if optimizer is None:
        optimizer = {}
    if not isinstance(optimizer, dict):
        raise CompilerOutputError("config.optimizer 必须是对象")
    optimize = optimizer.get("enabled", False)
    runs = optimizer.get("runs")
    if not isinstance(optimize, bool):
        raise CompilerOutputError("optimizer.enabled 必须是布尔值")
    if runs is not None and not isinstance(runs, int):
        raise CompilerOutputError("optimizer.runs 必须是整数")
    via_ir = cfg.get("viaIR", False)
    if not isinstance(via_ir, bool):
        raise CompilerOutputError("viaIR 必须是布尔值")
    evm = cfg.get("evmVersion")
    if evm is not None and not isinstance(evm, str):
        raise CompilerOutputError("evmVersion 必须是字符串")
    from .libraries import validate_libraries_structure

    libraries = validate_libraries_structure(cfg.get("libraries"))
    known = {"compilerVersion", "source", "name", "optimizer", "viaIR", "evmVersion", "libraries"}
    extra = {k: v for k, v in cfg.items() if k not in known}
    return CompilationConfig(
        compiler_version=version,
        source=source,
        name=name,
        evm_version=evm,
        optimize=optimize,
        optimize_runs=runs,
        via_ir=via_ir,
        libraries=libraries,
        extra_settings=extra,
    )


def parse_compiler_output(compiler_output: dict, source: str, name: str) -> ContractArtifact:
    """从 solc 标准 JSON 的 contracts[source][name] 取出目标合约产物。"""
    if not isinstance(compiler_output, dict):
        raise CompilerOutputError("compilerOutput 必须是 JSON 对象")
    contracts = compiler_output.get("contracts")
    if not isinstance(contracts, dict):
        raise CompilerOutputError("compilerOutput.contracts 缺失或不是对象")
    src_block = contracts.get(source)
    if not isinstance(src_block, dict):
        raise CompilerOutputError(f"compilerOutput 中找不到源文件条目: {source}")
    contract = src_block.get(name)
    if not isinstance(contract, dict):
        raise CompilerOutputError(f"compilerOutput 中找不到合约: {source}:{name}")

    metadata_str = _require(contract, "metadata", f"{source}:{name}", str)
    evm = contract.get("evm")
    if not isinstance(evm, dict):
        raise CompilerOutputError(f"{source}:{name} 缺少 evm 段")
    bytecode = evm.get("bytecode")
    deployed = evm.get("deployedBytecode")
    if not isinstance(bytecode, dict) or not isinstance(deployed, dict):
        raise CompilerOutputError(f"{source}:{name} 缺少 bytecode/deployedBytecode")

    from .bytecode import link_reference_regions, normalize_unlinked_hex

    dep_hex = normalize_unlinked_hex(deployed.get("object", ""))
    cre_hex = normalize_unlinked_hex(bytecode.get("object", ""))
    dep_refs = link_reference_regions(deployed.get("linkReferences", {}))
    cre_refs = link_reference_regions(bytecode.get("linkReferences", {}))

    from .metadata import parse_metadata_json

    metadata_obj, metadata_raw = parse_metadata_json(metadata_str)

    return ContractArtifact(
        source=source,
        name=name,
        metadata_raw=metadata_raw,
        metadata=metadata_obj,
        deployed_bytecode=dep_hex,
        deployed_link_refs=dep_refs,
        creation_bytecode=cre_hex,
        creation_link_refs=cre_refs,
        abi=contract.get("abi"),
    )
