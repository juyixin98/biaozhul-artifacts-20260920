"""制品执行器: 先验证, 验证通过才允许执行 entrypoint。

安全顺序是不可绕过的:
  1. verify_or_raise (任意一条问题 -> 抛异常, 进程非零退出);
  2. 再次确认 entrypoint 是清单内的普通可执行文件;
  3. 在制品根目录下执行它。

若 entrypoint 是解释器脚本 (如 app.py), 用 ``python3 <entrypoint>`` 调用;
若它本身带可执行位, 直接执行。本模块不提供任何 "跳过验证" 的开关。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .keys import TrustStore
from .safepaths import resolve_within
from .verify import VerifyReport, verify_or_raise


def verified_manifest(
    artifact_root: str | Path,
    envelope_path: str | Path,
    trust_dir: str | Path,
) -> VerifyReport:
    """加载信任库与封套并强制验证, 返回通过验证的 manifest dict。

    失败抛异常 —— 调用方绝不应 catch 后继续执行。
    """
    store = TrustStore.load(trust_dir)
    envelope_bytes = Path(envelope_path).read_bytes()
    report = verify_or_raise(
        artifact_root=artifact_root,
        envelope_bytes=envelope_bytes,
        trust_store=store,
        strict_extra=True,
        require_executable_entrypoint=False,  # 执行前自己再判一次
    )
    return report


def run(
    artifact_root: str | Path,
    envelope_path: str | Path,
    trust_dir: str | Path,
    entry_args: list[str] | None = None,
    interpreter: list[str] | None = None,
) -> int:
    """验证并执行制品 entrypoint, 返回退出码。

    参数:
      interpreter: 例如 ["python3"] 表示用解释器启动脚本;
                   None 时要求 entrypoint 自身可执行。
      entry_args:  追加给 entrypoint 的命令行参数。
    """
    report = verified_manifest(artifact_root, envelope_path, trust_dir)
    entrypoint = report.entrypoint
    if not entrypoint:
        raise ValueError("清单未声明 entrypoint, 无法执行")

    ep_real = resolve_within(artifact_root, entrypoint)
    if not ep_real.is_file():
        raise FileNotFoundError(f"entrypoint 不存在: {entrypoint}")

    if interpreter:
        argv = list(interpreter) + [str(ep_real)]
    else:
        if not os.access(ep_real, os.X_OK):
            raise PermissionError(
                f"entrypoint {entrypoint} 没有可执行权限; "
                "请指定解释器 (--python) 或 chmod +x"
            )
        argv = [str(ep_real)]
    argv.extend(entry_args or [])

    # cwd 固定为制品根; 继承环境变量 (本地工具, 不做额外净化以免破坏可用性)
    proc = subprocess.run(argv, cwd=str(Path(artifact_root).resolve()))
    return proc.returncode
