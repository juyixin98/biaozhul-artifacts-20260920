"""运行期配置：全部可通过环境变量覆盖，阈值集中管理以便审计。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    # 标定版本与上传图片的持久化目录
    storage_dir: str = os.getenv("CALIB_STORAGE_DIR", "./data")
    # HMAC 签名密钥（留空则在 storage_dir 下生成 0600 权限的随机密钥文件）
    hmac_secret_env: str = "CALIB_HMAC_SECRET"

    # ---- 样本与棋盘约束 ----
    min_board_dim: int = 3
    max_board_dim: int = 20
    # 通过初筛所需的最少去重视图数（经典标定推荐 >=10，这里默认 5 可配置）
    default_min_views: int = _get_int("CALIB_MIN_VIEWS", 5)
    hard_min_views: int = 3

    # ---- 重复视角聚类阈值（两视图同时满足三项才判为同视角）----
    dup_max_rotation_deg: float = _get_float("CALIB_DUP_ROT_DEG", 3.0)
    dup_max_translation_rel: float = _get_float("CALIB_DUP_TRANS_REL", 0.08)
    dup_max_center_rel: float = _get_float("CALIB_DUP_CENTER_REL", 0.04)

    # ---- 姿态覆盖退化阈值 ----
    min_max_pairwise_angle_deg: float = _get_float("CALIB_MIN_MAX_ANGLE_DEG", 12.0)
    min_mean_pairwise_angle_deg: float = _get_float("CALIB_MIN_MEAN_ANGLE_DEG", 4.0)
    min_fov_coverage_x: float = _get_float("CALIB_MIN_FOV_X", 0.25)
    min_fov_coverage_y: float = _get_float("CALIB_MIN_FOV_Y", 0.20)

    # ---- 离群剔除规则 ----
    outlier_abs_floor_px: float = _get_float("CALIB_OUTLIER_FLOOR_PX", 0.8)
    outlier_ratio_to_median: float = _get_float("CALIB_OUTLIER_RATIO", 3.0)
    max_rejection_rounds: int = 10

    # ---- 角点亚像素优化 ----
    subpix_win: int = 11
    subpix_max_iter: int = 40
    subpix_eps: float = 1e-3


settings = Settings()
