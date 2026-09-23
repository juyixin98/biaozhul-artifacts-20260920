# -*- coding: utf-8 -*-
"""统一业务错误（带机器可读错误码，便于测试断言）。"""
from __future__ import annotations


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


# 常用错误码（集中定义，避免拼写漂移）
ERR_UNKNOWN_CHAIN = "UNKNOWN_CHAIN"
ERR_UNKNOWN_CLIENT = "UNKNOWN_CLIENT"
ERR_UNKNOWN_CHANNEL = "UNKNOWN_CHANNEL"
ERR_UNKNOWN_CONNECTION = "UNKNOWN_CONNECTION"
ERR_UNKNOWN_PACKET = "UNKNOWN_PACKET"
ERR_DUPLICATE = "DUPLICATE_ENTITY"
ERR_BAD_INPUT = "BAD_INPUT"
ERR_INVALID_CHECKPOINT = "INVALID_CHECKPOINT"      # 缺失/签名错/链不匹配
ERR_STALE_CHECKPOINT = "STALE_CHECKPOINT"          # 高度回退或超出信任期
ERR_PROOF_VERIFY = "PROOF_VERIFICATION_FAILED"     # 证明重算根 != app_hash
ERR_PROOF_KEY = "PROOF_KEY_MISMATCH"               # 证明路径与期望状态键不符
ERR_PROOF_VALUE = "PROOF_VALUE_MISMATCH"           # 存在性/值不符合协议要求
ERR_CHANNEL_STATE = "CHANNEL_INVALID_STATE"
ERR_CHANNEL_CLOSED = "CHANNEL_CLOSED"
ERR_SEQ_GAP = "SEQUENCE_GAP"                       # 有序通道缺口等待
ERR_TIMEOUT = "PACKET_TIMED_OUT"                   # 收包时发现已超时
ERR_NOT_TIMEOUT = "PACKET_NOT_TIMED_OUT"
ERR_PACKET_FINALIZED = "PACKET_ALREADY_FINALIZED"  # 并发下终态互斥
ERR_BAD_PROOF_STRUCTURE = "BAD_PROOF_STRUCTURE"
ERR_VERSION_MISMATCH = "VERSION_MISMATCH"
ERR_ORDERING_MISMATCH = "ORDERING_MISMATCH"
