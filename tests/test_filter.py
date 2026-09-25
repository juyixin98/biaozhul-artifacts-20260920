"""核心滤波器测试：跟踪精度、连续缺测、奇异创新协方差、不同量纲、
对称性与半正定容差、输入校验。"""

import numpy as np
import pytest

from kalman_missing import KalmanFilter, KalmanInputError, Tolerances
from kalman_missing.filter import STATUS_OK, STATUS_PREDICT_ONLY, STATUS_SINGULAR
from kalman_missing.validation import MAX_STATE_DIM

from helpers import cv_model, simulate_cv

TOL = Tolerances()


def assert_psd_symmetric(P, tol=TOL):
    """验收不变量：对称性误差与最小特征值均在容差内。"""
    assert np.max(np.abs(P - P.T)) <= tol.sym_tol
    assert np.linalg.eigvalsh(P)[0] >= -tol.psd_tol


# ----------------------------------------------------------------------
# 验收 1：恒速轨迹跟踪精度
# ----------------------------------------------------------------------
def test_constant_velocity_tracking():
    states, zs = simulate_cv(n_steps=500, seed=1, r=1.0)
    F, Q, H, R = cv_model(r=1.0)
    kf = KalmanFilter(F, Q, H, R, x0=[0.0, 0.0], P0=np.eye(2) * 10.0)

    est = []
    for z in zs:
        res = kf.step([z])
        assert res.status == STATUS_OK
        assert_psd_symmetric(res.P)
        est.append(res.x)
    est = np.array(est)

    # 滤波后位置 RMSE 应显著小于直接量测 RMSE
    rmse_raw = np.sqrt(np.mean((np.array(zs) - states[:, 0]) ** 2))
    rmse_filt = np.sqrt(np.mean((est[:, 0] - states[:, 0]) ** 2))
    assert rmse_filt < 0.7 * rmse_raw
    # 速度在稳态段（跳过前 100 步瞬态）的 RMSE 应足够小
    rmse_vel = np.sqrt(np.mean((est[100:, 1] - states[100:, 1]) ** 2))
    assert rmse_vel < 0.3


# ----------------------------------------------------------------------
# 验收 2：连续缺测
# ----------------------------------------------------------------------
def test_consecutive_missing_measurements():
    states, zs = simulate_cv(n_steps=60, seed=2)
    F, Q, H, R = cv_model()
    kf = KalmanFilter(F, Q, H, R, x0=[0.0, 0.0], P0=np.eye(2) * 10.0)

    gap_start, gap_len = 25, 15          # 第 25~39 步全部缺测
    gap_results = []
    p_before_gap = None
    last_res = None
    for k, z in enumerate(zs):
        if gap_start <= k < gap_start + gap_len:
            res = kf.step(None)          # 整步缺测
            gap_results.append(res)
        else:
            res = kf.step([z])
            if k == gap_start - 1:
                p_before_gap = res.P
        assert_psd_symmetric(res.P)
        last_res = res

    # 缺测段全部标记为 predict_only
    assert all(r.status == STATUS_PREDICT_ONLY for r in gap_results)
    # 缺测期间协方差增长（不确定性累积）
    assert gap_results[0].P[0, 0] > p_before_gap[0, 0]
    assert gap_results[-1].P[0, 0] > gap_results[0].P[0, 0]
    # 缺测段按恒速模型外推，末端位置仍接近真值
    x_gap_end = gap_results[-1].x[0]
    assert abs(x_gap_end - states[gap_start + gap_len - 1, 0]) < 2.0
    # 缺测结束后量测恢复，滤波重新收敛
    assert last_res.status == STATUS_OK
    assert abs(last_res.x[0] - states[-1, 0]) < 1.0


def test_partial_missing_components():
    """部分缺测：双通道量测中一个通道缺测，另一通道仍被用于更新。"""
    F, Q, _, _ = cv_model()
    H = np.array([[1.0, 0.0], [0.0, 1.0]])     # 位置 + 速度双通道
    R = np.diag([0.25, 0.04])
    kf = KalmanFilter(F, Q, H, R, x0=[0.0, 1.0], P0=np.eye(2))

    res = kf.step([0.5, None])                 # 速度通道缺测
    assert res.status == STATUS_OK
    assert res.missing == [1]
    assert res.n_observed == 1
    assert_psd_symmetric(res.P)
    # 位置被拉向量测 0.5
    assert 0.0 < res.x[0] < 0.5 + 1.0

    res2 = kf.step([None, 1.1])                # 位置通道缺测
    assert res2.missing == [0]
    assert_psd_symmetric(res2.P)


# ----------------------------------------------------------------------
# 验收 3：奇异创新协方差
# ----------------------------------------------------------------------
def test_singular_innovation_covariance():
    """两个完全相同的无噪声位置传感器 -> S 秩亏，走伪逆回退路径。"""
    F, Q, _, _ = cv_model()
    H = np.array([[1.0, 0.0], [1.0, 0.0]])     # 两个相同的位置通道
    R = np.zeros((2, 2))                        # 无噪声 -> R 奇异
    kf = KalmanFilter(F, Q, H, R, x0=[5.0, 0.0], P0=np.eye(2))

    res = kf.step([3.0, 3.0])
    assert res.status == STATUS_SINGULAR
    assert np.all(np.isfinite(res.x))
    assert_psd_symmetric(res.P)
    # 无噪声位置量测：位置后验应精确等于量测，位置方差塌缩到 ~0
    assert abs(res.x[0] - 3.0) < 1e-8
    assert res.P[0, 0] < 1e-10

    # 后续步骤继续运行不发散
    for _ in range(10):
        res = kf.step([3.0, 3.0])
        assert_psd_symmetric(res.P)
    assert np.all(np.isfinite(res.x))


# ----------------------------------------------------------------------
# 验收 4：不同量纲（异质量测尺度）
# ----------------------------------------------------------------------
def test_mixed_units_measurements():
    """位置（米）与速度（毫米/秒）混合量测，噪声尺度相差 6 个数量级。"""
    F, Q, _, _ = cv_model()
    H = np.array([[1.0, 0.0], [0.0, 1000.0]])   # 位置 m，速度 m/s -> mm/s
    R = np.diag([0.25, 1.0e6])                  # 0.5 m 与 1000 mm/s (=1 m/s)
    kf = KalmanFilter(F, Q, H, R, x0=[0.0, 0.0], P0=np.eye(2) * 10.0)

    rng = np.random.default_rng(7)
    x_true = np.array([0.0, 1.0])
    for _ in range(100):
        x_true = F @ x_true
        z = [x_true[0] + 0.5 * rng.standard_normal(),
             1000.0 * x_true[1] + 1000.0 * rng.standard_normal()]
        res = kf.step(z)
        assert res.status == STATUS_OK
        assert_psd_symmetric(res.P)

    # 位置与速度均应收敛（速度通道经 1000 倍尺度映射后仍被正确融合）
    assert abs(res.x[0] - x_true[0]) < 0.5
    assert abs(res.x[1] - x_true[1]) < 0.3


# ----------------------------------------------------------------------
# 验收 5：对称性与半正定容差在长序列上保持
# ----------------------------------------------------------------------
def test_symmetry_psd_invariants_long_run():
    states, zs = simulate_cv(n_steps=500, seed=3)
    F, Q, H, R = cv_model()
    kf = KalmanFilter(F, Q, H, R, x0=[0.0, 0.0], P0=np.eye(2) * 10.0)
    for k, z in enumerate(zs):
        res = kf.step(None if k % 7 == 3 else [z])   # 周期性缺测
        assert_psd_symmetric(res.P)
        assert res.sym_err <= TOL.sym_tol


# ----------------------------------------------------------------------
# 输入校验与失败状态
# ----------------------------------------------------------------------
def test_validation_dimension_mismatch():
    F, Q, H, R = cv_model()
    with pytest.raises(KalmanInputError):
        KalmanFilter(F, Q, H, R, x0=[0.0, 0.0, 0.0], P0=np.eye(2))
    with pytest.raises(KalmanInputError):
        KalmanFilter(F, Q, np.ones((1, 3)), R, x0=[0.0, 0.0], P0=np.eye(2))
    with pytest.raises(KalmanInputError):
        KalmanFilter(F, np.ones((3, 3)), H, R, x0=[0.0, 0.0], P0=np.eye(2))


def test_validation_noise_must_be_symmetric_psd():
    F, Q, H, R = cv_model()
    Q_bad = Q.copy()
    Q_bad[0, 1] += 1e-6                        # 破坏对称性
    with pytest.raises(KalmanInputError):
        KalmanFilter(F, Q_bad, H, R, x0=[0.0, 0.0], P0=np.eye(2))

    R_bad = np.array([[-1.0]])                 # 负定噪声
    with pytest.raises(KalmanInputError):
        KalmanFilter(F, Q, H, R_bad, x0=[0.0, 0.0], P0=np.eye(2))

    P0_bad = np.diag([1.0, -1.0])              # 初始协方差非半正定
    with pytest.raises(KalmanInputError):
        KalmanFilter(F, Q, H, R, x0=[0.0, 0.0], P0=P0_bad)


def test_validation_non_finite_and_limits():
    F, Q, H, R = cv_model()
    F_nan = F.copy()
    F_nan[0, 0] = np.nan
    with pytest.raises(KalmanInputError):
        KalmanFilter(F_nan, Q, H, R, x0=[0.0, 0.0], P0=np.eye(2))

    # 超出小中规模上限
    n_big = MAX_STATE_DIM + 1
    with pytest.raises(KalmanInputError):
        KalmanFilter(np.eye(n_big), np.eye(n_big), np.ones((1, n_big)),
                     [[1.0]], np.zeros(n_big), np.eye(n_big))


def test_update_rejects_bad_z():
    F, Q, H, R = cv_model()
    kf = KalmanFilter(F, Q, H, R, x0=[0.0, 0.0], P0=np.eye(2))
    kf.predict()
    with pytest.raises(KalmanInputError):
        kf.update([1.0, 2.0])                  # 维数错误
    with pytest.raises(KalmanInputError):
        kf.update([np.inf])                    # 非有限（非缺测语义）
    # NaN 视为缺测，合法
    res = kf.update([np.nan])
    assert res.status == STATUS_PREDICT_ONLY


def test_tolerance_validation():
    with pytest.raises(KalmanInputError):
        Tolerances(sym_tol=-1e-10)
    with pytest.raises(KalmanInputError):
        Tolerances(rcond=0.0)
