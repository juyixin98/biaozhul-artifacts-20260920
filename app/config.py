"""集中配置：全部可由环境变量覆盖。"""
from __future__ import annotations

import os


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


class Settings:
    # 迟到消息允许窗口（秒）。测量时间早于“最新已见测量时间 - horizon”即拒绝。
    LATE_HORIZON_S: float = _get_float("FUSER_HORIZON_S", 2.0)

    # 过程噪声强度（连续白噪声加速度模型，单位 ~ m^2/s^3）
    PROCESS_NOISE_Q: float = _get_float("FUSER_Q", 1.0)
    # 初始化时位置/速度协方差
    INIT_POS_VAR: float = _get_float("FUSER_INIT_POS_VAR", 100.0)
    INIT_VEL_VAR: float = _get_float("FUSER_INIT_VEL_VAR", 100.0)

    # 马氏距离（NIS = innovation' S^-1 innovation）门限，chi^2_2 的 99% 分位数
    GATE_NIS: float = _get_float("FUSER_GATE_NIS", 9.2103)

    # 协方差合法性容差
    COV_SYM_TOL: float = _get_float("FUSER_COV_SYM_TOL", 1e-8)
    COV_PSD_TOL: float = _get_float("FUSER_COV_PSD_TOL", 1e-9)

    # HMAC-SHA256 共享密钥（生产环境务必通过 FUSER_HMAC_SECRET 注入）
    HMAC_SECRET: str = os.environ.get(
        "FUSER_HMAC_SECRET", "dev-shared-secret-change-me"
    )
    # 签名时间戳新鲜度（秒）
    SIGN_FRESHNESS_S: float = _get_float("FUSER_SIGN_FRESHNESS_S", 300.0)

    # 审计与轨迹的留存上限（条数）
    MAX_REJECTIONS: int = int(os.environ.get("FUSER_MAX_REJECTIONS", "4096"))


settings = Settings()
