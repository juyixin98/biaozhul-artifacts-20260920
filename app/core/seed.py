"""主随机种子的持有、冻结与持有证明。

设计要点
--------
1. 创建研究时**必须**显式提供种子（``SEED_REQUIRED``），服务永不静默
   "随机生成一个"——实验者必须知道并冻结自己研究的种子。
2. 种子本身在内存中以 :class:`SecretSeed` 持有；持久层只保存
   ``fingerprint``（用于把契约与种子绑定）与 ``proof``（PBKDF2 持有证明，
   便于审计在不知道种子的情况下验证"配置里的种子确实是当初那颗"）。
3. 校验种子时使用恒定时间比较，避免计时侧信道。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from ..errors import AppError, ErrorCategory

PBKDF2_ITERATIONS = 200_000
SEED_MIN_BYTES = 16          # 128 bit 最小熵
SEED_MAX_BYTES = 1024
FINGERPRINT_SALT = b"strat-block-rct/seed-fingerprint/v1"
PROOF_SALT_PREFIX = b"strat-block-rct/seed-proof/v1:"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


@dataclass(frozen=True)
class SeedCommitment:
    """落盘的种子承诺：不含种子明文。"""

    fingerprint: str
    proof: str

    def to_dict(self) -> dict:
        return {"fingerprint": self.fingerprint, "proof": self.proof}


class SecretSeed:
    """内存中的主种子。禁止通过 repr / 日志泄漏（见 ``__repr__``）。"""

    __slots__ = ("_material",)

    def __init__(self, material: bytes):
        if not isinstance(material, (bytes, bytearray)):
            raise AppError(
                ErrorCategory.INVALID_SEED, 400,
                "种子必须是字节串（建议以 base64 提供并解码）",
            )
        material = bytes(material)
        if not (SEED_MIN_BYTES <= len(material) <= SEED_MAX_BYTES):
            raise AppError(
                ErrorCategory.INVALID_SEED, 400,
                f"种子长度必须在 {SEED_MIN_BYTES}..{SEED_MAX_BYTES} 字节之间",
                {"length": len(material)},
            )
        object.__setattr__(self, "_material", material)

    @classmethod
    def from_base64(cls, text: str) -> "SecretSeed":
        if not isinstance(text, str) or not text:
            raise AppError(
                ErrorCategory.SEED_REQUIRED, 400,
                "必须提供 seed_base64；服务不会隐式生成主种子",
            )
        try:
            raw = _unb64(text)
        except Exception:
            raise AppError(
                ErrorCategory.INVALID_SEED, 400,
                "seed_base64 不是合法的 base64",
            )
        return cls(raw)

    @classmethod
    def generate(cls) -> "SecretSeed":
        """仅供夹具/测试使用：显式生成 32 字节种子。生产路径不自动调用。"""
        return cls(secrets.token_bytes(32))

    @property
    def material(self) -> bytes:
        return self._material

    def fingerprint(self) -> str:
        digest = hmac.new(FINGERPRINT_SALT, self._material, hashlib.sha256).digest()
        return "seed_" + digest.hex()

    def proof(self, *, iterations: int = PBKDF2_ITERATIONS) -> str:
        """PBKDF2 持有证明：``pbkdf2-sha256$iter$salt_b64$hash_b64``。

        盐每次重新生成，因此证明只用于"同一颗种子"的可验证核对，
        不作为指纹。
        """
        salt = secrets.token_bytes(16)
        derived = hashlib.pbkdf2_hmac(
            "sha256", self._material, PROOF_SALT_PREFIX + salt, iterations
        )
        return f"pbkdf2-sha256${iterations}${_b64(salt)}${_b64(derived)}"

    def commit(self) -> SeedCommitment:
        return SeedCommitment(fingerprint=self.fingerprint(), proof=self.proof())

    def verify_proof(self, proof: str) -> bool:
        """用本种子核对落盘的持有证明（恒定时间）。"""
        try:
            scheme, iter_text, salt_b64, hash_b64 = proof.split("$")
            if scheme != "pbkdf2-sha256":
                return False
            iterations = int(iter_text)
            salt = _unb64(salt_b64)
            expected = _unb64(hash_b64)
        except Exception:
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256", self._material, PROOF_SALT_PREFIX + salt, iterations
        )
        return hmac.compare_digest(derived, expected)

    def __repr__(self) -> str:
        # 绝不允许在日志/traceback 中打出种子
        return f"<SecretSeed:0x{id(self):x} withheld fingerprint={self.fingerprint()[:12]}…>"
