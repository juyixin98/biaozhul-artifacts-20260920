"""密钥版本状态机模型。

状态机（单向，销毁不可逆）：

    GENERATED --activate--> ACTIVE --deactivate--> DEACTIVATED --destroy--> DESTROYED
    GENERATED --destroy--> DESTROYED
    DEACTIVATED --activate--> ACTIVE   （允许回切，便于轮换演练）

规则：
  * 加密只允许使用 ACTIVE 版本；
  * 解密允许 ACTIVE 与 DEACTIVATED（历史版本权限）；
  * DESTROYED 后密钥材料被擦除，密文明确不可恢复。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional


class KeyState(str, enum.Enum):
    GENERATED = "GENERATED"
    ACTIVE = "ACTIVE"
    DEACTIVATED = "DEACTIVATED"
    DESTROYED = "DESTROYED"


# 允许的状态迁移表
ALLOWED_TRANSITIONS: Dict[KeyState, frozenset] = {
    KeyState.GENERATED: frozenset({KeyState.ACTIVE, KeyState.DESTROYED}),
    KeyState.ACTIVE: frozenset({KeyState.DEACTIVATED}),
    KeyState.DEACTIVATED: frozenset({KeyState.ACTIVE, KeyState.DESTROYED}),
    KeyState.DESTROYED: frozenset(),
}

# 各状态是否允许解密（历史版本权限）
DECRYPT_ALLOWED = frozenset({KeyState.ACTIVE, KeyState.DEACTIVATED})


@dataclass
class KeyMetadata:
    """密钥版本的元数据（不含密钥材料本身）。"""

    version_id: str
    state: KeyState
    created_at: str
    activated_at: Optional[str] = None
    deactivated_at: Optional[str] = None
    destroyed_at: Optional[str] = None
    algorithm: str = "AES-256-GCM"
    # 密钥材料指纹（SHA-256），用于审计与完整性核对，本身不可逆推出密钥
    key_fingerprint: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "KeyMetadata":
        d = dict(d)
        d["state"] = KeyState(d["state"])
        return cls(**d)
