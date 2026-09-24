"""相机标定核心：真实 OpenCV 角点检测、相机标定、重投影误差、
重复视角聚类、姿态覆盖评估与离群剔除。

设计原则：
* 不使用任何固定/预置内参矩阵替代真实计算；初值仅为 solvePnP 的粗猜测，
  最终内参来自 cv2.calibrateCamera 的实际优化结果。
* 每一张图、每一轮剔除都留下可追溯记录（rejected_images / rejection_history）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .config import settings


class CalibrationError(Exception):
    """业务可解释的标定失败。code 稳定、reason 面向调用方。"""

    def __init__(self, code: str, reason: str, **extra: object) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.extra = extra


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class ImageObservation:
    image_id: str
    filename: str
    width: int
    height: int
    corners: np.ndarray  # (N,1,2) float32 —— OpenCV 期望的形状
    rvec: np.ndarray | None = None
    tvec: np.ndarray | None = None
    rms_px: float | None = None


# --------------------------------------------------------------------------- #
# 棋盘与图像
# --------------------------------------------------------------------------- #
def validate_board(pattern_cols: int, pattern_rows: int, square_size_mm: float) -> None:
    if pattern_cols < settings.min_board_dim or pattern_rows < settings.min_board_dim:
        raise CalibrationError(
            "INVALID_BOARD",
            f"棋盘内角点数过小：({pattern_cols}x{pattern_rows})，"
            f"两个方向都必须 >= {settings.min_board_dim}",
        )
    if pattern_cols > settings.max_board_dim or pattern_rows > settings.max_board_dim:
        raise CalibrationError(
            "INVALID_BOARD",
            f"棋盘内角点数过大：({pattern_cols}x{pattern_rows})，"
            f"上限为 {settings.max_board_dim}",
        )
    if square_size_mm <= 0:
        raise CalibrationError(
            "INVALID_BOARD", f"棋盘格边长必须为正数，收到 {square_size_mm} mm"
        )


def decode_image(raw: bytes) -> np.ndarray:
    """字节 -> BGR 图像；损坏/空数据抛 CalibrationError。"""
    if not raw:
        raise CalibrationError("DECODE_FAILED", "上传文件为空（0 字节）")
    arr = np.frombuffer(raw, dtype=np.uint8)
    try:
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except cv2.error:
        img = None
    if img is None:
        raise CalibrationError("DECODE_FAILED", "图像无法解码（非受支持的图片格式或文件损坏）")
    return img


def object_points(pattern_cols: int, pattern_rows: int, square_size_mm: float) -> np.ndarray:
    """棋盘角点的三维物体坐标，z=0，单位毫米，形状 (N,1,3) float32。"""
    pts = np.zeros((pattern_cols * pattern_rows, 3), np.float32)
    pts[:, :2] = np.mgrid[0:pattern_cols, 0:pattern_rows].T.reshape(-1, 2)
    pts[:, :2] *= float(square_size_mm)
    return pts.reshape(-1, 1, 3)


_PROBE_MAX_DIM = 800
_TEXTURE_GATE_LAPLACIAN_VAR = 15.0


def detect_corners(gray: np.ndarray, pattern_cols: int, pattern_rows: int):
    """检测棋盘内角点并做亚像素精化。

    策略（兼顾速度与准确性，各阶段结果在 flags_used 中如实标注）：
    1) 纹理闸门：拉普拉斯方差过低（模糊/渐变/弱纹理图）直接判无棋盘，
       避免 OpenCV 在此类图上极慢的穷举；
    2) 全分辨率严格检测（自适应阈值+归一化+四边形过滤），失败则放宽过滤重试；
    3) 仍失败则在降采样图（长边<=800）快速探测，防止透视过强时严格模式漏检；
       探针命中后回到全分辨率再检一次并亚像素精化（降采样仅用于“有无”判定，
       不用于角点定位，避免尺寸假阳性）。
    返回 (corners(N,1,2) float32, flags_used)；失败返回 (None, flags)。
    """
    pattern_size = (pattern_cols, pattern_rows)
    base_flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    strict_flags = base_flags | cv2.CALIB_CB_FILTER_QUADS

    texture = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if texture < _TEXTURE_GATE_LAPLACIAN_VAR:
        return None, f"texture_gate(laplacian_var={texture:.2f})"

    found, corners = cv2.findChessboardCorners(gray, pattern_size, strict_flags)
    flags_used = "adaptive_thresh|normalize|filter_quads"
    if not found:
        found, corners = cv2.findChessboardCorners(gray, pattern_size, base_flags)
        flags_used += "->relaxed"
    if not found:
        # 降采样探针：仅判定有无，命中后必须回到全分辨率重新定位
        scale = min(1.0, _PROBE_MAX_DIM / float(max(gray.shape[1], gray.shape[0])))
        if scale < 1.0:
            probe = cv2.resize(gray, None, fx=scale, fy=scale,
                               interpolation=cv2.INTER_AREA)
            probe_hit, _ = cv2.findChessboardCorners(probe, pattern_size, base_flags)
            flags_used += f"->probe@{scale:.2f}:{'hit' if probe_hit else 'miss'}"
            if probe_hit:
                found, corners = cv2.findChessboardCorners(gray, pattern_size,
                                                           base_flags)
    if not found:
        return None, flags_used

    criteria = (
        cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
        settings.subpix_max_iter,
        settings.subpix_eps,
    )
    win = (settings.subpix_win, settings.subpix_win)
    corners = cv2.cornerSubPix(gray, corners, win, (-1, -1), criteria)
    return corners.astype(np.float32), flags_used


def suggest_board_dimensions(
    gray: np.ndarray, pattern_cols: int, pattern_rows: int
) -> list[dict]:
    """角点失败时的可追溯诊断：转置优先，再按切比雪夫距离环（半径 2）展开，
    在降采样探针图上检测（单次 <=~1s），报告能检测到的尺寸。
    """
    suggestions: list[dict] = []
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    if float(cv2.Laplacian(gray, cv2.CV_64F).var()) < _TEXTURE_GATE_LAPLACIAN_VAR:
        return suggestions  # 无纹理图任何尺寸都不可能检出
    scale = min(1.0, _PROBE_MAX_DIM / float(max(gray.shape[1], gray.shape[0])))
    probe = (cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
             if scale < 1.0 else gray)

    ring: dict[int, list[tuple[int, int]]] = {}
    for dc in range(-2, 3):
        for dr in range(-2, 3):
            if dc == 0 and dr == 0:
                continue
            ring.setdefault(max(abs(dc), abs(dr)), []).append(
                (pattern_cols + dc, pattern_rows + dr)
            )
    # 候选排序：转置最先；其余按切比雪夫距离、再按列差优先（行列数填错通常单列）
    candidates = [(pattern_rows, pattern_cols)]
    rest = [(c, r) for dist in sorted(ring) for (c, r) in ring[dist]]
    rest.sort(key=lambda cr: (max(abs(cr[0] - pattern_cols), abs(cr[1] - pattern_rows)),
                              cr[1] != pattern_rows,
                              abs(cr[0] - pattern_cols)))
    candidates.extend(rest)

    seen: set[tuple[int, int]] = {(pattern_cols, pattern_rows)}
    tried = 0
    for idx, (c, r) in enumerate(candidates):
        if (c, r) in seen:
            continue
        seen.add((c, r))
        if not (settings.min_board_dim <= c <= settings.max_board_dim
                and settings.min_board_dim <= r <= settings.max_board_dim):
            continue
        tried += 1
        # 转置与切比雪夫距离 1 的候选用全分辨率（最可能且数量少）；
        # 距离 2 的候选才用降采样探针（降采样可能漏检角点，故只用于兜底）
        search_img = gray if idx <= 8 else probe
        try:
            ok, _ = cv2.findChessboardCorners(search_img, (c, r), flags)
        except cv2.error:
            ok = False
        if ok:
            suggestions.append({"pattern_cols": c, "pattern_rows": r, "detected": True})
            if len(suggestions) >= 3:
                break
        if tried >= 10:
            break
    return suggestions


# --------------------------------------------------------------------------- #
# 位姿与几何
# --------------------------------------------------------------------------- #
def initial_poses(
    objp: np.ndarray,
    image_points: list[np.ndarray],
    width: int,
    height: int,
):
    """标定前用粗猜测内参逐张解 PnP，用于去重与姿态覆盖初筛。

    猜测矩阵 K=[w,0,w/2; 0,w,h/2; 0,0,1] 仅用于解算相对姿态，绝不进入最终结果。
    返回与输入等长、顺序对齐的 rvec/tvec 列表（失败项为 None）。
    """
    k_guess = np.array(
        [[float(width), 0.0, width / 2.0],
         [0.0, float(width), height / 2.0],
         [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    rvecs: list = []
    tvecs: list = []
    for ip in image_points:
        ok, rvec, tvec = cv2.solvePnP(objp, ip, k_guess, None,
                                      flags=cv2.SOLVEPNP_ITERATIVE)
        if ok:
            rvecs.append(rvec)
            tvecs.append(tvec)
        else:
            rvecs.append(None)
            tvecs.append(None)
    return rvecs, tvecs


def rotation_angle_deg(r1: np.ndarray, r2: np.ndarray) -> float:
    """两个旋转向量之间的夹角（度）：angle(R1^T R2)。"""
    R1, _ = cv2.Rodrigues(r1)
    R2, _ = cv2.Rodrigues(r2)
    Rrel = R1.T @ R2
    acos_arg = float(np.clip((np.trace(Rrel) - 1.0) / 2.0, -1.0, 1.0))
    return math.degrees(math.acos(acos_arg))


def translation_relative_distance(t1: np.ndarray, t2: np.ndarray) -> float:
    """平移差异相对于平均到棋盘距离的比值（尺度归一，避免与 mm 单位耦合）。"""
    a = t1.reshape(3).astype(np.float64)
    b = t2.reshape(3).astype(np.float64)
    scale = (np.linalg.norm(a) + np.linalg.norm(b)) / 2.0
    if scale < 1e-9:
        return float("inf")
    return float(np.linalg.norm(a - b) / scale)


def cluster_duplicate_views(
    obs: list[ImageObservation],
) -> tuple[list[int], list[list[int]]]:
    """贪心聚类重复视角。

    两视图被判重复需【同时】满足：
      旋转夹角 <= dup_max_rotation_deg
      平移相对差 <= dup_max_translation_rel
      投影中心相对偏移 <= dup_max_center_rel（归一化焦距）
    每个簇保留首张（通常也是误差较小的一张），其余标记 duplicate。
    返回 (保留索引, 簇列表)。
    """
    n = len(obs)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    for i in range(n):
        for j in range(i + 1, n):
            assert obs[i].rvec is not None and obs[j].rvec is not None
            rot_d = rotation_angle_deg(obs[i].rvec, obs[j].rvec)
            trans_d = translation_relative_distance(obs[i].tvec, obs[j].tvec)
            center_d = projection_center_relative(obs[i], obs[j])
            if (
                rot_d <= settings.dup_max_rotation_deg
                and trans_d <= settings.dup_max_translation_rel
                and center_d <= settings.dup_max_center_rel
            ):
                union(i, j)

    clusters_map: dict[int, list[int]] = {}
    for i in range(n):
        clusters_map.setdefault(find(i), []).append(i)
    clusters = list(clusters_map.values())
    kept = [c[0] for c in clusters]
    return sorted(kept), sorted(clusters, key=lambda c: c[0])


def projection_center_relative(a: ImageObservation, b: ImageObservation) -> float:
    """两图角点质心的归一化画面中心偏移（用宽度归一，近似焦距尺度）。"""
    ca = a.corners.reshape(-1, 2).mean(axis=0)
    cb = b.corners.reshape(-1, 2).mean(axis=0)
    return float(np.linalg.norm(ca - cb) / max(float(a.width), 1.0))


# --------------------------------------------------------------------------- #
# 覆盖评估
# --------------------------------------------------------------------------- #
def compute_coverage(
    obs: list[ImageObservation], width: int, height: int
) -> dict:
    """姿态覆盖指标：视角间最大/平均夹角、角点对画面 X/Y 的覆盖比例。

    覆盖不足（姿态集中在同一方位/角落）会使标定病态，这里量化输出供规则判定。
    """
    angles: list[float] = []
    for i in range(len(obs)):
        for j in range(i + 1, len(obs)):
            assert obs[i].rvec is not None and obs[j].rvec is not None
            angles.append(rotation_angle_deg(obs[i].rvec, obs[j].rvec))

    if angles:
        max_pair_angle = max(angles)
        mean_pair_angle = sum(angles) / len(angles)
    else:
        max_pair_angle = 0.0
        mean_pair_angle = 0.0

    all_corners = np.concatenate([o.corners.reshape(-1, 2) for o in obs], axis=0)
    mins = all_corners.min(axis=0)
    maxs = all_corners.max(axis=0)
    span_x = float((maxs[0] - mins[0]) / max(width, 1))
    span_y = float((maxs[1] - mins[1]) / max(height, 1))
    # 平均质心是否落在画面中部附近（分布偏置），仅作报告字段
    centers = np.array([o.corners.reshape(-1, 2).mean(axis=0) for o in obs])
    center_spread = centers.std(axis=0) / np.array([width, height], dtype=np.float64)

    return {
        "view_count": len(obs),
        "max_pairwise_rotation_deg": round(max_pair_angle, 4),
        "mean_pairwise_rotation_deg": round(mean_pair_angle, 4),
        "corner_span_x_ratio": round(span_x, 4),
        "corner_span_y_ratio": round(span_y, 4),
        "centroid_spread_x_ratio": round(float(center_spread[0]), 4),
        "centroid_spread_y_ratio": round(float(center_spread[1]), 4),
    }


def coverage_violations(coverage: dict) -> list[str]:
    """根据配置阈值给出覆盖退化原因列表（空列表表示通过）。"""
    violations: list[str] = []
    if coverage["max_pairwise_rotation_deg"] < settings.min_max_pairwise_angle_deg:
        violations.append(
            f"视角间最大旋转角 {coverage['max_pairwise_rotation_deg']:.2f}° "
            f"< {settings.min_max_pairwise_angle_deg:.2f}°，姿态方向覆盖退化"
        )
    if coverage["mean_pairwise_rotation_deg"] < settings.min_mean_pairwise_angle_deg:
        violations.append(
            f"视角间平均旋转角 {coverage['mean_pairwise_rotation_deg']:.2f}° "
            f"< {settings.min_mean_pairwise_angle_deg:.2f}°，视角过度集中"
        )
    if coverage["corner_span_x_ratio"] < settings.min_fov_coverage_x:
        violations.append(
            f"角点横向覆盖 {coverage['corner_span_x_ratio']:.2%} 画面 "
            f"< 阈值 {settings.min_fov_coverage_x:.0%}"
        )
    if coverage["corner_span_y_ratio"] < settings.min_fov_coverage_y:
        violations.append(
            f"角点纵向覆盖 {coverage['corner_span_y_ratio']:.2%} 画面 "
            f"< 阈值 {settings.min_fov_coverage_y:.0%}"
        )
    return violations


# --------------------------------------------------------------------------- #
# 标定与逐图重投影误差
# --------------------------------------------------------------------------- #
def run_calibration(
    objp: np.ndarray, obs: list[ImageObservation], width: int, height: int
) -> dict:
    """真实执行 cv2.calibrateCamera（5 参数畸变模型），并逐图计算重投影 RMSE。"""
    img_points = [o.corners for o in obs]
    obj_points_list = [objp for _ in obs]
    img_size = (width, height)

    rms, camera_matrix, dist_coeffs, rvecs, tvecs = cv2.calibrateCamera(
        obj_points_list,
        img_points,
        img_size,
        None,
        None,
        flags=0,
        criteria=(
            cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
            200,
            1e-8,
        ),
    )

    per_image: list[float] = []
    total_sq = 0.0
    total_n = 0
    for o, ip, rvec, tvec in zip(obs, img_points, rvecs, tvecs):
        projected, _ = cv2.projectPoints(objp, rvec, tvec, camera_matrix, dist_coeffs)
        err = projected.reshape(-1, 2) - ip.reshape(-1, 2)
        sq = float(np.sum(err ** 2))
        rmse = math.sqrt(sq / err.shape[0])
        per_image.append(rmse)
        total_sq += sq
        total_n += err.shape[0]
        o.rms_px = rmse
        o.rvec = rvec
        o.tvec = tvec

    overall = math.sqrt(total_sq / total_n)
    return {
        "rms_overall": overall,
        "camera_matrix": camera_matrix,
        "dist_coeffs": dist_coeffs,
        "rvecs": rvecs,
        "tvecs": tvecs,
        "per_image_rms": per_image,
    }


def pose_summary(rvec: np.ndarray, tvec: np.ndarray) -> dict:
    """把位姿转成可 JSON 化、可审计的形式。"""
    return {
        "rvec": [round(float(v), 8) for v in rvec.reshape(3)],
        "tvec_mm": [round(float(v), 6) for v in tvec.reshape(3)],
    }
