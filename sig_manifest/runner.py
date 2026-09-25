"""验证通过后的制品执行器。

**安全边界**：本模块只做一件事——先跑完整验证，只有 ``ok=True`` 才拉起
entrypoint 子进程。任何验证错误（签名错误、未知密钥、摘要不符、文件缺失、
路径逃逸……）都会在 ``subprocess`` 被调用之前抛出异常。

执行约束：
- 工作目录固定为制品根目录（realpath 之后）；
- argv 直接取自已签名的 ``signed.entrypoint``，运行时不允许追加/覆盖
  其中的程序路径（``extra_args`` 只允许追加在末尾）；
- 默认以最小环境启动，仅保留必要变量，避免宿主机环境影响制品行为；
- 入口脚本的 shebang 与可执行位由操作系统解释；Windows 风格路径不支持。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .errors import RunnerError
from .keys import TrustStore
from .paths import resolve_within
from .verify import VerificationReport, verify_artifact_or_raise

# 默认保留的环境变量白名单（足够大多数解释器脚本运行）。
_DEFAULT_ENV_KEEP = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "HOME",
    "TMPDIR",
)


@dataclass
class RunResult:
    returncode: int
    verified_key_ids: list[str]
    argv: list[str]
    env: dict[str, str]


def verify_then_run(
    artifact_root: str | os.PathLike[str],
    manifest_text: str | bytes | dict[str, Any],
    trust_store: TrustStore,
    *,
    extra_args: Sequence[str] | None = None,
    env_extra: dict[str, str] | None = None,
    clean_env: bool = True,
    timeout: float | None = None,
    dry_run: bool = False,
) -> RunResult:
    """先验证，通过后执行 entrypoint。验证失败时本函数保证不创建子进程。

    timeout 仅用于测试/防护；None 表示由调用方控制。
    dry_run=True 时完成全部准备但不实际 spawn（供自动化测试断言
    "验证失败绝不执行" 与 "验证通过才会走到 spawn 前一步"）。
    """
    report: VerificationReport = verify_artifact_or_raise(
        artifact_root, manifest_text, trust_store
    )

    root_real = Path(artifact_root).resolve(strict=True)
    signed = _signed_of(manifest_text)
    entrypoint = signed.get("entrypoint")
    if not entrypoint:
        raise RunnerError("清单未声明 entrypoint，无法执行")
    entrypoint = list(entrypoint)

    # 再次解析入口路径（verify 已查过；这里保证 spawn 使用的就是 realpath）。
    resolved_target = resolve_within(root_real, entrypoint[0])
    if not resolved_target.is_file():
        raise RunnerError(f"entrypoint 目标不存在: {entrypoint[0]}")

    argv = list(entrypoint)
    if extra_args:
        for a in extra_args:
            if not isinstance(a, str):
                raise RunnerError("extra_args 必须全部是字符串")
            argv.append(a)

    env = _build_env(clean_env=clean_env, env_extra=env_extra)
    # 显式注入验证上下文，制品程序可记录是按哪张清单运行的。
    env["SIG_MANIFEST_ARTIFACT"] = str(root_real)
    env["SIG_MANIFEST_KEY_IDS"] = ",".join(report.verified_key_ids)

    if dry_run:
        return RunResult(
            returncode=0,
            verified_key_ids=report.verified_key_ids,
            argv=argv,
            env=env,
        )

    try:
        proc = subprocess.run(  # noqa: S603 - argv 来自已签名清单，不经 shell
            argv,
            cwd=str(root_real),
            env=env,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        raise RunnerError(f"无法启动 entrypoint {argv[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(f"entrypoint 执行超时（{timeout}s）") from exc

    return RunResult(
        returncode=proc.returncode,
        verified_key_ids=report.verified_key_ids,
        argv=argv,
        env=env,
    )


def _signed_of(manifest_text: str | bytes | dict[str, Any]) -> dict[str, Any]:
    if isinstance(manifest_text, dict):
        return manifest_text["signed"]
    from .manifest import parse_envelope

    return parse_envelope(manifest_text)["signed"]


def _build_env(*, clean_env: bool, env_extra: dict[str, str] | None) -> dict[str, str]:
    if clean_env:
        env: dict[str, str] = {}
        for name in _DEFAULT_ENV_KEEP:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
    else:
        env = dict(os.environ)
    if env_extra:
        for k, v in env_extra.items():
            if not isinstance(k, str) or not k or "=" in k or "\x00" in k:
                raise RunnerError(f"非法环境变量名: {k!r}")
            if "\x00" in v:
                raise RunnerError(f"环境变量 {k} 的值包含 NUL")
            env[k] = v
    return env
