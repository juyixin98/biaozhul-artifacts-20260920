"""离线分析主流程：预处理 -> 峰值检测 -> 静止识别 -> 稳健零偏估计 -> 置信评估。"""

from __future__ import annotations

import numpy as np

from .config import RuntimeConfig
from .detection import StaticInterval, Window, detect_spikes, detect_static
from .estimator import fit_axis
from .preprocess import CleanData, preprocess

AXES = ("x", "y", "z")
GRAVITY = 9.80665

# 仅凭静止数据不可完整识别的参数（响应中固定声明，防止误报“已标定”）
UNOBSERVABLE_PARAMS = [
    {
        "parameter": "accelerometer_bias",
        "reason": "静止时测量满足 a_meas = R^T*g + b_a，静止段只给出 |a_meas|≈g 的标量约束，"
        "重力方向上的加表零偏与姿态小角耦合，垂直重力方向的两个零偏分量完全不可观测；"
        "需要多个已知/未知差异方位的六面静止或外部姿态激励才能标定。",
    },
    {
        "parameter": "accelerometer_scale_factor_and_misalignment",
        "reason": "刻度因子与非正交参数在 |真实比力|≈常数（仅重力）时与零偏、姿态不可分；"
        "需要变化比力幅值/方向的激励。",
    },
    {
        "parameter": "gyroscope_scale_factor",
        "reason": "静止时真实角速度恒为 0，测量为 g_meas = S*(ω+b)，S 乘以 0 不产生信息，"
        "刻度因子完全不可辨识；需要已知转速旋转。",
    },
    {
        "parameter": "gyroscope_misalignment_nonorthogonality",
        "reason": "非正交矩阵在 ω=0 时同样不产生约束，需要绕各轴的已知旋转激励。",
    },
    {
        "parameter": "gyroscope_g_sensitivity",
        "reason": "加速度敏感项（g-sensitivity）与零偏、重力方向耦合；单一方位静止无法分离，"
        "需要多方位静止且各方位零偏可独立观测。",
    },
    {
        "parameter": "time_and_temperature_drift_simultaneous",
        "reason": "单调升温的静止数据中时间与温度强共线，时漂系数与温漂系数无法同时辨识；"
        "响应在两者都可用时只选择解释力更强的一个并给出告警。",
    },
]


def _window_center(w: Window) -> float:
    return 0.5 * (w.t0 + w.t1)


def _predict(fit: dict, t: float, temp: float | None) -> float:
    v = fit["bias_at_reference"]
    if fit["model"] == "time_drift":
        v += fit["slope"] * (t - fit["t_reference_s"])
    elif fit["model"] == "temp_drift" and temp is not None:
        v += fit["slope"] * (temp - fit["temperature_reference_C"])
    return v


def _quality(
    n_windows: int,
    total_duration: float,
    max_scatter: float,
    seg_bias_ranges: np.ndarray | None,
    cfg: RuntimeConfig,
) -> dict:
    c_windows = 1.0 if n_windows >= 30 else 0.6 if n_windows >= 10 else 0.3 if n_windows >= 4 else 0.0
    c_duration = (
        1.0 if total_duration >= 30 else 0.6 if total_duration >= 10 else 0.3
    )
    c_scatter = (
        1.0 if max_scatter <= 0.002 else 0.6 if max_scatter <= 0.01 else 0.3 if max_scatter <= 0.05 else 0.0
    )
    if seg_bias_ranges is not None and len(seg_bias_ranges) >= 2:
        worst = float(np.max(seg_bias_ranges))
        c_agreement = (
            1.0 if worst <= 0.005 else 0.6 if worst <= 0.02 else 0.3 if worst <= 0.05 else 0.0
        )
        agreement_note = f"跨静止段零偏最大差异 {worst:.5f} rad/s"
    else:
        c_agreement = 0.5
        agreement_note = "仅有一个含静止的时间段，无法评估跨段重复性"

    score = 0.25 * c_windows + 0.25 * c_duration + 0.35 * c_scatter + 0.15 * c_agreement
    grade = "high" if score >= 0.8 else "medium" if score >= 0.5 else "low"
    return {
        "grade": grade,
        "score": round(float(score), 3),
        "components": {
            "window_count": c_windows,
            "static_duration": c_duration,
            "window_scatter": c_scatter,
            "cross_segment_reproducibility": c_agreement,
        },
        "note": agreement_note,
        "interpretation": "high=零偏可直接用于去偏；medium=可用但建议增加静止时长/方位复核；"
        "low=样本不足或离散过大，零偏仅供参考。",
    }


def run_pipeline(raw, cfg: RuntimeConfig) -> dict:
    """raw 为 EstimateRequest 的字段（已通过 pydantic 校验）。"""
    timestamps = np.asarray(raw.timestamps, dtype=float)
    accel = np.asarray(raw.accel, dtype=float)
    gyro = np.asarray(raw.gyro, dtype=float)
    temp = (
        np.asarray(raw.temperature, dtype=float)
        if raw.temperature is not None
        else None
    )

    data: CleanData = preprocess(timestamps, accel, gyro, temp, cfg)
    spikes = detect_spikes(data.gyro, data.accel, cfg.spike_z)
    windows, intervals, det_summary = detect_static(data, cfg)

    warnings: list[str] = []
    if spikes["total_samples_flagged"]:
        warnings.append(
            f"检测到 {spikes['total_samples_flagged']} 个异常峰值样本（稳健 z>{cfg.spike_z:g}）；"
            "这些样本通过方差准则被排除在静止估计之外，请核查传感器或采集链路。"
        )
    if data.preprocess["duplicate_samples_dropped_inconsistent"]:
        warnings.append(
            f"{data.preprocess['duplicate_samples_dropped_inconsistent']} 个重复时间戳样本组内不一致，已整组剔除。"
        )
    if data.preprocess["time_gaps"]:
        warnings.append(
            f"检测到 {data.preprocess['time_gaps']} 个时间缺口，已切分为 "
            f"{len(data.segments)} 段独立检测。"
        )

    # 标记每个接受窗属于哪个静止区间
    accepted: list[tuple[int, Window]] = []
    for w_idx, w in enumerate(windows):
        for iv in intervals:
            if (
                w.segment_index == iv.segment_index
                and iv.start <= w.start
                and w.end <= iv.end
            ):
                accepted.append((w_idx, w))
                break

    seg_records = []
    seg_const_bias: dict[int, np.ndarray] = {}

    for seg in data.segments:
        sw = [w for _, w in accepted if w.segment_index == seg.index]
        rec = {
            "segment_index": seg.index,
            "t_start_s": seg.t_start,
            "t_end_s": seg.t_end,
            "duration_s": seg.duration,
            "n_samples": seg.n_samples,
            "n_static_windows": len(sw),
            "static_duration_s": round(
                sum(w.t1 - w.t0 for w in sw), 6
            ),
        }
        if sw:
            ys = np.array([w.gyro_med for w in sw])
            cnts = np.array([w.n for w in sw], dtype=float)
            wmean = np.average(ys, axis=0, weights=cnts)
            rec["gyro_bias_constant_rad_s"] = [float(v) for v in wmean]
            seg_const_bias[seg.index] = wmean
        seg_records.append(rec)

    intervals_out = []
    for iid, iv in enumerate(intervals):
        iw = [w for _, w in accepted if w.segment_index == iv.segment_index and iv.start <= w.start and w.end <= iv.end]
        a_med = np.median(np.array([w.accel_med for w in iw]), axis=0) if iw else np.zeros(3)
        intervals_out.append(
            {
                "interval_id": iid,
                "segment_index": iv.segment_index,
                "t_start_s": iv.t0,
                "t_end_s": iv.t1,
                "duration_s": round(iv.duration, 6),
                "n_windows": iv.n_windows,
                "n_samples": iv.n_samples,
                "observed_specific_force_xyz_ms2": [float(v) for v in a_med],
                "gravity_magnitude_residual_ms2": round(float(np.linalg.norm(a_med) - GRAVITY), 6),
                "note": "该矢量是静止时测得的比力（含重力投影与未知加表零偏），不是加表零偏估计值。",
            }
        )

    gyro_bias_out: dict
    accel_out: dict
    confidence: dict
    window_residuals: list[dict] = []

    if not accepted:
        warnings.append(
            "未识别到任何满足准则的静止区间，陀螺零偏无法由本次数据估计；"
            "请检查阈值、传感器是否全程运动，或补充静止采集。"
        )
        gyro_bias_out = {
            "status": "not_observable_from_data",
            "reason": "无静止候选窗，静止假设（ω_true=0）在本数据中没有可用支撑。",
            "axes": {},
        }
        confidence = {
            "grade": "low",
            "score": 0.0,
            "components": {},
            "note": "无静止样本，零偏不可估。",
        }
        accel_out = {"status": "not_estimated", "observed_static": intervals_out}
    else:
        wa = [w for _, w in accepted]
        tc = np.array([_window_center(w) for w in wa])
        ys = np.array([w.gyro_med for w in wa])
        cnts = np.array([w.n for w in wa], dtype=float)
        tmp = np.array([w.temperature_mean if w.temperature_mean is not None else np.nan for w in wa])
        tmp_fit = tmp if np.all(np.isfinite(tmp)) else None

        axes_fits = {}
        axis_weights = {}
        for ai, ax in enumerate(AXES):
            fit_d, win_w = fit_axis(tc, tmp_fit, ys[:, ai], cnts, cfg)
            axes_fits[ax] = fit_d
            axis_weights[ax] = win_w / cnts  # 归一化 Huber 降权 ∈ [0,1]
        chosen_models = {ax: axes_fits[ax]["model"] for ax in AXES}

        # 共线性告警
        if all(
            axes_fits[ax]["time_drift_eligible"] and axes_fits[ax]["temp_drift_eligible"]
            for ax in AXES
        ):
            warnings.append(
                "时间与温度漂移模型同时满足拟合条件（静止段内二者强相关），"
                "无法同时辨识时漂与温漂；结果按加权 SSE 取其一，另一个未报告系数。"
            )
        for ax in AXES:
            if axes_fits[ax]["model"] != "constant":
                warnings.append(
                    f"轴 {ax.upper()} 选择了 {axes_fits[ax]['model']} 模型；"
                    "报告的常数零偏是参考点处的外推值，使用时应连同斜率一起在工作温度/时刻上计算。"
                )

        # 逐窗残差（基于各轴选定模型的预测值）
        for k, (_, w) in enumerate(accepted):
            pred = np.array(
                [
                    _predict(
                        axes_fits[ax],
                        _window_center(w),
                        w.temperature_mean,
                    )
                    for ax in AXES
                ]
            )
            resid = w.gyro_med - pred
            window_residuals.append(
                {
                    "segment_index": w.segment_index,
                    "t0_s": round(w.t0, 6),
                    "t1_s": round(w.t1, 6),
                    "gyro_median_rad_s": [float(v) for v in w.gyro_med],
                    "gyro_residual_rad_s": [float(v) for v in resid],
                    "gyro_residual_norm_rad_s": float(np.linalg.norm(resid)),
                    "gyro_std_rad_s": [float(v) for v in w.gyro_std],
                    "accel_norm_residual_ms2": round(
                        w.accel_norm_med - GRAVITY, 6
                    ),
                    "temperature_mean_C": w.temperature_mean,
                    "huber_weight_xyz": [
                        round(float(axis_weights[ax][k]), 4) for ax in AXES
                    ],
                }
            )

        bias_ref = np.array([axes_fits[ax]["bias_at_reference"] for ax in AXES])
        bias_se = np.array([axes_fits[ax]["bias_se"] for ax in AXES])
        max_scatter = float(max(axes_fits[ax]["robust_residual_scale"] for ax in AXES))

        # 跨段重复性
        seg_ranges = None
        if len(seg_const_bias) >= 2:
            mat = np.array(list(seg_const_bias.values()))
            seg_ranges = np.ptp(mat, axis=0)
            if float(np.max(seg_ranges)) > 0.02:
                warnings.append(
                    "各静止段常数零偏差异较大（可能存在温漂/振动残留或段间标定变化），"
                    "已在置信度中降分；优先查看带漂移模型的结果。"
                )

        total_static = float(sum(iv.duration for iv in intervals))
        confidence = _quality(len(wa), total_static, max_scatter, seg_ranges, cfg)
        confidence.update(
            {
                "n_static_windows": len(wa),
                "n_static_intervals": len(intervals),
                "n_segments_with_static": len(seg_const_bias),
                "total_static_duration_s": round(total_static, 3),
                "max_robust_window_scatter_rad_s": round(max_scatter, 7),
                "bias_standard_error_rad_s": [float(v) for v in bias_se],
                "bias_95ci_halfwidth_rad_s": [float(1.96 * v) for v in bias_se],
            }
        )

        gyro_bias_out = {
            "status": "estimated",
            "reference": {
                "t_reference_s": {ax: axes_fits[ax]["t_reference_s"] for ax in AXES},
                "temperature_reference_C": {
                    ax: axes_fits[ax]["temperature_reference_C"] for ax in AXES
                },
            },
            "bias_at_reference_rad_s": [float(v) for v in bias_ref],
            "bias_at_reference_deg_s": [float(np.degrees(v)) for v in bias_ref],
            "standard_error_rad_s": [float(v) for v in bias_se],
            "ci95_rad_s": [
                [float(b - 1.96 * s), float(b + 1.96 * s)]
                for b, s in zip(bias_ref, bias_se)
            ],
            "chosen_model_per_axis": chosen_models,
            "axes": axes_fits,
        }

        # 多方位检测
        grav_dirs = []
        for iv in intervals_out:
            v = np.array(iv["observed_specific_force_xyz_ms2"])
            if np.linalg.norm(v) > 1e-9:
                grav_dirs.append(v / np.linalg.norm(v))
        max_angle_deg = None
        if len(grav_dirs) >= 2:
            gd = np.array(grav_dirs)
            cosang = np.clip(gd @ gd.T, -1.0, 1.0)
            np.fill_diagonal(cosang, 1.0)
            max_angle_deg = float(np.degrees(np.arccos(np.min(cosang))))

        accel_out = {
            "status": "bias_not_identifiable_from_static_only",
            "observed_static": intervals_out,
            "orientation_spread_deg": max_angle_deg,
            "multi_orientation_detected": max_angle_deg is not None and max_angle_deg > 10.0,
            "explanation": "静止观测为比力矢量（重力投影 + 未知加表零偏）。"
            "即便检测到多个方位，本服务也不在此外报告加表零偏数值——"
            "多方位静止标定需要足够姿态覆盖与联合优化，超出本次仅基于静止方差的识别范围。",
        }
        if max_angle_deg is not None and max_angle_deg > 10.0:
            warnings.append(
                f"静止区间之间最大姿态夹角约 {max_angle_deg:.1f}°（多方位采集），"
                "但加表零偏仍未被当作已求解参数报告。"
            )

    window_records = [
        {
            "segment_index": w.segment_index,
            "t0_s": round(w.t0, 6),
            "t1_s": round(w.t1, 6),
            "is_static": w.static,
            "in_static_interval": any(
                w.segment_index == iv.segment_index
                and iv.start <= w.start
                and w.end <= iv.end
                for iv in intervals
            ),
            "gyro_std_rad_s": [float(v) for v in w.gyro_std],
            "accel_std_ms2": [float(v) for v in w.accel_std],
            "accel_norm_med_ms2": round(w.accel_norm_med, 6),
            "reject_reasons": w.reject_reasons,
        }
        for w in windows
    ]

    return {
        "ok": True,
        "schema_version": "1.0",
        "summary": {
            "input_samples": data.preprocess["input_samples"],
            "kept_samples": data.preprocess["kept_samples"],
            "segments": len(data.segments),
            "static_intervals": len(intervals),
            "windows_total": det_summary["windows_total"],
            "windows_in_static_intervals": det_summary["windows_in_static_intervals"],
            "gyro_bias_status": gyro_bias_out["status"],
        },
        "config_echo": {
            "units": raw.units.model_dump(),
            "axes": raw.axes.model_dump(),
            "detection_thresholds_si": {
                "window_seconds": cfg.window_seconds,
                "window_overlap": cfg.window_overlap,
                "min_static_seconds": cfg.min_static_seconds,
                "gyro_std_thresh_rad_s": cfg.gyro_std_thresh,
                "accel_std_thresh_ms2": cfg.accel_std_thresh,
                "gravity_mag_tol_ms2": cfg.gravity_mag_tol,
                "time_gap_factor": cfg.time_gap_factor,
                "spike_z": cfg.spike_z,
                "gravity_reference_ms2": GRAVITY,
            },
        },
        "preprocessing": data.preprocess,
        "detection_summary": det_summary,
        "spikes": spikes,
        "segments": seg_records,
        "static_intervals": intervals_out,
        "windows": window_records,
        "window_residuals": window_residuals,
        "gyroscope_bias": gyro_bias_out,
        "accelerometer": accel_out,
        "unobservable_parameters": UNOBSERVABLE_PARAMS,
        "confidence": confidence,
        "warnings": warnings,
    }
