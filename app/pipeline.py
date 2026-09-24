"""标定流水线：从上传字节到可签名、可追溯的标定版本记录。"""
from __future__ import annotations

import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from .calibration import (
    CalibrationError,
    ImageObservation,
    cluster_duplicate_views,
    compute_coverage,
    coverage_violations,
    decode_image,
    detect_corners,
    initial_poses,
    object_points,
    pose_summary,
    run_calibration,
    suggest_board_dimensions,
    validate_board,
)
from .config import settings
from .crypto import sha256_bytes, sign_payload


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _threshold_snapshot() -> dict:
    return {
        "min_views": settings.default_min_views,
        "hard_min_views": settings.hard_min_views,
        "duplicate": {
            "max_rotation_deg": settings.dup_max_rotation_deg,
            "max_translation_relative": settings.dup_max_translation_rel,
            "max_projection_center_relative": settings.dup_max_center_rel,
        },
        "coverage": {
            "min_max_pairwise_angle_deg": settings.min_max_pairwise_angle_deg,
            "min_mean_pairwise_angle_deg": settings.min_mean_pairwise_angle_deg,
            "min_corner_span_x_ratio": settings.min_fov_coverage_x,
            "min_corner_span_y_ratio": settings.min_fov_coverage_y,
        },
        "outlier": {
            "abs_floor_px": settings.outlier_abs_floor_px,
            "ratio_to_median": settings.outlier_ratio_to_median,
            "max_rounds": settings.max_rejection_rounds,
        },
        "subpix": {
            "window": settings.subpix_win,
            "max_iter": settings.subpix_max_iter,
            "epsilon": settings.subpix_eps,
        },
    }


def _rejected(image_id: str, filename: str, stage: str, reason: str,
              rms: float | None = None, detail: dict | None = None) -> dict:
    return {
        "image_id": image_id,
        "filename": filename,
        "stage": stage,
        "reason": reason,
        "rms_px": round(rms, 6) if rms is not None else None,
        "detail": detail or {},
    }


def calibrate_from_uploads(
    files: list[tuple[str, bytes]],
    camera_id: str,
    pattern_cols: int,
    pattern_rows: int,
    square_size_mm: float,
    min_views: int | None,
    storage_dir: str,
) -> dict:
    """执行完整离线标定，成功返回版本记录 dict；失败抛 CalibrationError。

    files: [(filename, raw_bytes), ...]
    """
    validate_board(pattern_cols, pattern_rows, square_size_mm)
    required_views = min_views or settings.default_min_views
    if required_views < settings.hard_min_views:
        required_views = settings.hard_min_views

    if len(files) < settings.hard_min_views:
        raise CalibrationError(
            "INSUFFICIENT_SAMPLES",
            f"仅提交 {len(files)} 张图片，硬性下限为 {settings.hard_min_views} 张",
        )

    rejected: list[dict] = []
    detections: list[dict] = []
    file_meta: dict[str, dict] = {}
    raw_by_image: dict[str, bytes] = {}

    # ---- 1. 解码 ---- #
    decoded: list[tuple[str, str, np.ndarray, bytes]] = []
    ref_size: tuple[int, int] | None = None
    for filename, raw in files:
        image_id = uuid.uuid4().hex[:12]
        file_meta[image_id] = {
            "filename": filename,
            "sha256": sha256_bytes(raw),
            "bytes": len(raw),
        }
        raw_by_image[image_id] = raw
        try:
            img = decode_image(raw)
        except CalibrationError as exc:
            rejected.append(_rejected(image_id, filename, "decode", exc.reason))
            detections.append({"image_id": image_id, "filename": filename, "detected": False,
                               "stage": "decode", "reason": exc.reason})
            continue
        h, w = img.shape[:2]
        if ref_size is None:
            ref_size = (w, h)
        if (w, h) != ref_size:
            reason = (f"分辨率 {w}x{h} 与同批基准 {ref_size[0]}x{ref_size[1]} 不一致；"
                      "标定版本严格绑定单一分辨率，不能混合尺寸")
            rejected.append(_rejected(image_id, filename, "resolution", reason,
                                      detail={"width": w, "height": h,
                                              "reference": list(ref_size)}))
            detections.append({"image_id": image_id, "filename": filename, "detected": False,
                               "stage": "resolution", "width": w, "height": h})
            continue
        decoded.append((image_id, filename, img, raw))

    if ref_size is None:
        raise CalibrationError(
            "ALL_IMAGES_UNREADABLE",
            "没有任何图片可解码，无法进行标定",
            rejected_images=rejected, detections=detections,
            thresholds=_threshold_snapshot(),
        )

    width, height = ref_size

    # ---- 2. 角点检测 ---- #
    obs_list: list[ImageObservation] = []
    suggestion_diagnostic_budget = 2
    for image_id, filename, img, _raw in decoded:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        corners, flags_used = detect_corners(gray, pattern_cols, pattern_rows)
        if corners is None:
            detail = {"flags_tried": flags_used,
                      "declared_board": [pattern_cols, pattern_rows]}
            reason = "未检测到完整棋盘内角点"
            if suggestion_diagnostic_budget > 0:
                suggestion_diagnostic_budget -= 1
                suggestions = suggest_board_dimensions(gray, pattern_cols, pattern_rows)
                detail["nearby_boards_detected"] = suggestions
                if suggestions:
                    alt = suggestions[0]
                    reason += (f"；声明 {pattern_cols}x{pattern_rows} 可能有误，"
                               f"图像上可检测到 {alt['pattern_cols']}x{alt['pattern_rows']}")
                else:
                    reason += "；邻近尺寸也未检出，图像可能模糊/无棋盘/对比度不足"
            rejected.append(_rejected(image_id, filename, "corner", reason, detail=detail))
            detections.append({"image_id": image_id, "filename": filename, "detected": False,
                               "stage": "corner", "flags": flags_used})
            continue
        obs_list.append(ImageObservation(image_id, filename, width, height, corners))
        detections.append({"image_id": image_id, "filename": filename, "detected": True,
                           "flags": flags_used, "corner_count": int(corners.shape[0])})

    if len(obs_list) < settings.hard_min_views:
        raise CalibrationError(
            "INSUFFICIENT_SAMPLES",
            f"仅 {len(obs_list)} 张图片成功检测到角点，硬性下限为 "
            f"{settings.hard_min_views} 张",
            resolution=[width, height], detections=detections,
            rejected_images=rejected, thresholds=_threshold_snapshot(),
        )

    # ---- 3. 初始位姿（PnP，用于去重与覆盖初筛）---- #
    objp = object_points(pattern_cols, pattern_rows, square_size_mm)
    rvecs, tvecs = initial_poses(
        objp, [o.corners for o in obs_list], width, height
    )
    pnp_failed = [idx for idx, r in enumerate(rvecs) if r is None]
    if pnp_failed:
        still_good: list[ImageObservation] = []
        good_rvecs: list = []
        good_tvecs: list = []
        for idx, o in enumerate(obs_list):
            if rvecs[idx] is None:
                rejected.append(_rejected(o.image_id, o.filename, "corner",
                                          "角点检测通过但 solvePnP 位姿解算失败（几何不自洽）"))
            else:
                still_good.append(o)
                good_rvecs.append(rvecs[idx])
                good_tvecs.append(tvecs[idx])
        obs_list = still_good
        rvecs, tvecs = good_rvecs, good_tvecs
    for o, rvec, tvec in zip(obs_list, rvecs, tvecs):
        o.rvec, o.tvec = rvec, tvec

    # ---- 4. 重复视角聚类 ---- #
    kept_idx, clusters = cluster_duplicate_views(obs_list)
    kept_set = set(kept_idx)
    cluster_by_member: dict[int, list[int]] = {}
    for cid, cluster in enumerate(clusters):
        for member in cluster:
            cluster_by_member[member] = cluster
    deduped: list[ImageObservation] = []
    for idx, o in enumerate(obs_list):
        if idx in kept_set:
            deduped.append(o)
        else:
            cluster = cluster_by_member[idx]
            representative = obs_list[cluster[0]]
            rejected.append(_rejected(
                o.image_id, o.filename, "duplicate",
                f"与样本 {representative.image_id}（{representative.filename}）视角重复："
                f"旋转差≤{settings.dup_max_rotation_deg}°、平移相对差"
                f"≤{settings.dup_max_translation_rel}、投影中心偏移"
                f"≤{settings.dup_max_center_rel} 三项同时成立",
                detail={"cluster_id": cluster,
                        "representative_image_id": representative.image_id},
            ))
    obs_list = deduped

    if len(obs_list) < required_views:
        raise CalibrationError(
            "INSUFFICIENT_DISTINCT_VIEWS",
            f"去重后仅 {len(obs_list)} 个不同视角，要求至少 {required_views} 个；"
            f"共 {len(rejected)} 张因解码/分辨率/角点/重复被剔除。"
            "请增加相对相机方向和位置明显不同的棋盘图像",
            resolution=[width, height], detections=detections,
            rejected_images=rejected, rejected_count=len(rejected),
            accepted_count=len(obs_list), thresholds=_threshold_snapshot(),
        )

    # ---- 5. 覆盖初筛 ---- #
    coverage = compute_coverage(obs_list, width, height)
    violations = coverage_violations(coverage)
    if violations:
        raise CalibrationError(
            "POSE_DEGENERACY",
            "姿态覆盖退化，拒绝出结果：" + "；".join(violations),
            resolution=[width, height], detections=detections,
            rejected_images=rejected, coverage=coverage,
            rejected_count=len(rejected), accepted_count=len(obs_list),
            thresholds=_threshold_snapshot(),
        )

    # ---- 6. 标定 + 迭代离群剔除 ---- #
    rejection_history: list[dict] = []
    calib = run_calibration(objp, obs_list, width, height)
    for round_no in range(1, settings.max_rejection_rounds + 1):
        rms_values = list(calib["per_image_rms"])
        median_rms = statistics.median(rms_values)
        threshold = max(settings.outlier_abs_floor_px,
                        settings.outlier_ratio_to_median * median_rms)
        worst_pos = int(np.argmax(rms_values))
        worst_rms = rms_values[worst_pos]
        if worst_rms <= threshold:
            break
        worst_obs = obs_list[worst_pos]
        rejected.append(_rejected(
            worst_obs.image_id, worst_obs.filename, "outlier",
            f"逐图重投影误差 {worst_rms:.4f}px 超过剔除阈值 {threshold:.4f}px"
            f"（max(绝对下限 {settings.outlier_abs_floor_px}px, "
            f"{settings.outlier_ratio_to_median}×中位数 {median_rms:.4f}px)）",
            rms=worst_rms,
            detail={"round": round_no, "median_rms_px": round(median_rms, 6),
                    "threshold_px": round(threshold, 6),
                    "overall_rms_before_px": round(calib["rms_overall"], 6)},
        ))
        rejection_history.append({
            "round": round_no,
            "removed_image_id": worst_obs.image_id,
            "removed_filename": worst_obs.filename,
            "rms_px": round(worst_rms, 6),
            "median_rms_px": round(median_rms, 6),
            "threshold_px": round(threshold, 6),
            "remaining": len(obs_list) - 1,
        })
        obs_list.pop(worst_pos)
        if len(obs_list) < required_views:
            raise CalibrationError(
                "INSUFFICIENT_VIEWS_AFTER_REJECTION",
                f"离群剔除后仅剩 {len(obs_list)} 张（要求 {required_views} 张），"
                "样本不足以支撑可信标定；请补充高质量图像而非放宽阈值",
                resolution=[width, height], detections=detections,
                rejected_images=rejected, rejected_count=len(rejected),
                accepted_count=len(obs_list), thresholds=_threshold_snapshot(),
            )
        calib = run_calibration(objp, obs_list, width, height)

    # ---- 7. 最终覆盖复核（剔除可能破坏覆盖）---- #
    coverage = compute_coverage(obs_list, width, height)
    violations = coverage_violations(coverage)
    if violations:
        raise CalibrationError(
            "POSE_DEGENERACY",
            "离群剔除后姿态覆盖退化，拒绝出结果：" + "；".join(violations),
            resolution=[width, height], detections=detections,
            rejected_images=rejected, coverage=coverage,
            rejected_count=len(rejected), accepted_count=len(obs_list),
            thresholds=_threshold_snapshot(),
        )

    # ---- 8. 组装版本记录 ---- #
    K = calib["camera_matrix"]
    dist = calib["dist_coeffs"].reshape(-1)
    intrinsics = {
        "camera_matrix": [[float(v) for v in row] for row in K],
        "fx": float(K[0, 0]), "fy": float(K[1, 1]),
        "cx": float(K[0, 2]), "cy": float(K[1, 2]),
    }
    distortion = {
        "model": "plumb_bob_5",
        "coeffs": [float(v) for v in dist[:5]],
        "k1": float(dist[0]), "k2": float(dist[1]),
        "p1": float(dist[2]), "p2": float(dist[3]), "k3": float(dist[4]),
    }
    accepted_images = [
        {
            "image_id": o.image_id,
            "filename": o.filename,
            "rms_px": round(float(o.rms_px), 6),
            "pose": pose_summary(o.rvec, o.tvec),
        }
        for o in obs_list
    ]
    accepted_ids = {o.image_id for o in obs_list}

    version_id = uuid.uuid4().hex
    record = {
        "version_id": version_id,
        "camera_id": camera_id,
        "resolution": [width, height],
        "board": {
            "pattern_cols": pattern_cols,
            "pattern_rows": pattern_rows,
            "square_size_mm": float(square_size_mm),
            "inner_corner_count": pattern_cols * pattern_rows,
        },
        "created_at": _now_iso(),
        "image_count": len(files),
        "accepted_count": len(obs_list),
        "rejected_count": len(rejected),
        "overall_rms_px": round(float(calib["rms_overall"]), 6),
        "intrinsics": intrinsics,
        "distortion": distortion,
        "coverage": coverage,
        "accepted_images": accepted_images,
        "rejected_images": rejected,
        "rejection_history": rejection_history,
        "thresholds": _threshold_snapshot(),
        "provenance": {
            "opencv_version": cv2.__version__,
            "numpy_version": np.__version__,
            "engine": "cv2.calibrateCamera",
            "distortion_model_flags": 0,
            "input_files": [
                {
                    "image_id": mid,
                    "filename": meta["filename"],
                    "sha256": meta["sha256"],
                    "bytes": meta["bytes"],
                    "used": mid in accepted_ids,
                }
                for mid, meta in file_meta.items()
            ],
        },
        "state": "active",
    }
    record["signature"] = sign_payload(
        {k: v for k, v in record.items() if k != "signature"}, storage_dir
    )

    _persist_input_images(Path(storage_dir), version_id, raw_by_image)
    return record


def _persist_input_images(
    storage: Path, version_id: str, raw_by_image: dict[str, bytes]
) -> None:
    """把本次输入原始字节落盘，保证样本可复核（目录 0700，文件 0600）。"""
    img_dir = storage / "images" / version_id
    img_dir.mkdir(parents=True, exist_ok=True)
    img_dir.chmod(0o700)
    for image_id, raw in raw_by_image.items():
        target = img_dir / f"{image_id}.png"
        target.write_bytes(raw)
        target.chmod(0o600)
