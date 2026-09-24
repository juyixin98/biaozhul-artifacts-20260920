"""离线评测夹具与指标：交叉运动 / 短时遮挡 / 重复检测 / 空帧。

重要边界：本模块生成的真值对象 ID（truth_id）**只用于离线打分**，
``feed_frames`` 提交给跟踪器的只有 (x, y, detection_id) 三元组，
detection_id 是观测端的字符串标签（如 "A-3"），关联器从不读取它做匹配。

指标
----
* id_switches：每条真值轨迹对应已确认轨迹 ID 的变更次数（首次建立不算）。
* rmse / mean_error：已确认轨迹预测位置与最近真值之间的欧氏误差。
* confirmed_tracks：最终存活/曾经确认的轨迹集合。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .tracker import Tracker, TrackerConfig

DT = 1.0
# 加噪鲁棒性测试使用 sigma=0.15（见 run_evaluation.py --noisy），
# 默认确定性夹具近似无噪，阈值可复现。


@dataclass
class TruthObject:
    truth_id: str
    xy: np.ndarray  # (T, 2) 真值位置，含 NaN 表示该帧不可见（遮挡/空）


@dataclass
class Scenario:
    name: str
    description: str
    objects: list[TruthObject]
    frames: list  # list[list[(x,y,detection_id)]]，可能含重复点
    timestamps: list[float]
    notes: dict = field(default_factory=dict)
    # 逐帧的噪声共享簇大小：按检测出现顺序切分，同一簇内的副本共享一次
    # 噪声实现（模拟同一物理检测被重复输出）。None 表示每检测独立成簇。
    duplicate_cluster_sizes: list[list[int]] | None = None


# ------------------------------------------------------------- scenario makers
def _noise(rng: np.random.Generator, n: int, std: float) -> np.ndarray:
    return rng.normal(0.0, std, size=(n, 2))


def make_crossing(rng: np.random.Generator) -> Scenario:
    """两条直线交叉、不同时到达交叉点（避免完全重叠的歧义）。

    A: 左下 -> 右上；B: 左上 -> 右下。B 晚 2 帧经过交叉点，
    最小同时距离约 2.83（远大于噪声与门控尺度），不应发生 ID 切换。
    """
    T = 21
    t = np.arange(T)
    a = np.stack([0.0 + 1.0 * t, 0.0 + 1.0 * t], axis=1)
    # B 几何轨迹 x+y=20，速度 (1,-1)，与 A 在 (10,10) 交叉，且晚 2 帧到
    b = np.stack([8.0 + 1.0 * t, 12.0 - 1.0 * t], axis=1)
    frames = []
    for k in range(T):
        frames.append(
            [
                (float(a[k, 0]), float(a[k, 1]), f"A-{k}"),
                (float(b[k, 0]), float(b[k, 1]), f"B-{k}"),
            ]
        )
    return Scenario(
        "crossing",
        "两条直线交叉且错时经过交叉点，检验身份保持（ID switch = 0）",
        [TruthObject("A", a), TruthObject("B", b)],
        frames,
        list(t * DT),
        {"closest_simultaneous_distance": "~2.83"},
    )


def make_short_occlusion(rng: np.random.Generator, gap: int = 4) -> Scenario:
    """一个目标在中段连续 ``gap`` 帧不可见（短于 max_misses），应保持同一 ID。"""
    T = 25
    t = np.arange(T)
    p = np.stack([0.0 + 0.8 * t, np.full(T, 5.0)], axis=1)
    truth = p.copy()
    truth[10 : 10 + gap] = np.nan
    frames = []
    for k in range(T):
        if 10 <= k < 10 + gap:
            frames.append([])
        else:
            frames.append([(float(p[k, 0]), float(p[k, 1]), f"A-{k}")])
    return Scenario(
        f"occlusion_gap{gap}",
        f"目标中段连续 {gap} 帧完全遮挡（滑行），重获后身份保持",
        [TruthObject("A", truth)],
        frames,
        list(t * DT),
        {"gap_frames": list(range(10, 10 + gap))},
    )


def make_duplicate_detections(rng: np.random.Generator) -> Scenario:
    """每帧同一目标产生 2 个很近的重复检测（应聚合，不新增轨迹 ID）。"""
    T = 15
    t = np.arange(T)
    p = np.stack([0.0 + 0.7 * t, 1.0 + 0.2 * t], axis=1)
    frames = []
    for k in range(T):
        # 两个副本相距 ~0.05（远小于 merge_radius=0.25）
        frames.append(
            [
                (float(p[k, 0]), float(p[k, 1]), f"A-{k}-a"),
                (float(p[k, 0] + 0.04), float(p[k, 1] - 0.03), f"A-{k}-b"),
            ]
        )
    return Scenario(
        "duplicates",
        "每帧重复检测同一目标，检验帧内聚合与幂等（始终 1 条轨迹）",
        [TruthObject("A", p)],
        frames,
        list(t * DT),
        {"duplicates_per_frame": 2},
        duplicate_cluster_sizes=[[2] if frame else [] for frame in frames],
    )


def make_empty_frames(rng: np.random.Generator) -> Scenario:
    """开头及穿插空帧：目标在第 5 帧才出现，中间偶发漏检。"""
    T = 22
    t = np.arange(T)
    p = np.stack([2.0 + 0.6 * (t - 5), np.full(T, -3.0)], axis=1)
    missing = {0, 1, 2, 3, 4, 9, 16}  # 出生前空帧 + 两次单发漏检
    truth = p.copy()
    for k in missing:
        truth[k] = np.nan
    frames = []
    for k in range(T):
        if k in missing:
            frames.append([])
        else:
            frames.append([(float(p[k, 0]), float(p[k, 1]), f"A-{k}")])
    return Scenario(
        "empty_frames",
        "出生前连续空帧 + 运行中穿插空帧，不应产生轨迹或错误删除",
        [TruthObject("A", truth)],
        frames,
        list(t * DT),
        {"empty_frames": sorted(missing)},
    )


def all_scenarios(seed: int = 20260923) -> list[Scenario]:
    rng = np.random.default_rng(seed)
    return [
        make_crossing(rng),
        make_short_occlusion(rng, gap=4),
        make_short_occlusion(rng, gap=6),
        make_duplicate_detections(rng),
        make_empty_frames(rng),
    ]


# --------------------------------------------------------------------- runner
@dataclass
class Metrics:
    scenario: str
    id_switches: int
    rmse: float
    mean_error: float
    max_error: float
    matched_frames: int
    confirmed_ids_used: list[int]
    final_alive_tracks: int
    births_total: int
    deleted_total: int

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "id_switches": self.id_switches,
            "rmse": self.rmse,
            "mean_error": self.mean_error,
            "max_error": self.max_error,
            "matched_frames": self.matched_frames,
            "confirmed_ids_used": self.confirmed_ids_used,
            "final_alive_tracks": self.final_alive_tracks,
            "births_total": self.births_total,
            "deleted_total": self.deleted_total,
        }


def run_scenario(
    scenario: Scenario,
    config: TrackerConfig | None = None,
    measurement_std: float = 0.0,
    seed: int = 0,
) -> tuple[Metrics, list]:
    """运行夹具并离线打分。

    ``measurement_std > 0`` 时对提交检测额外加高斯噪声（默认夹具本身近似无噪，
    评测使用确定性数据以便给出可复现阈值）。
    返回 (Metrics, 每步 FrameResult)。
    """
    cfg = config or TrackerConfig()
    tracker = Tracker(cfg)
    rng = np.random.default_rng(seed)

    results = []
    for frame_id, (ts, dets) in enumerate(
        zip(scenario.timestamps, scenario.frames)
    ):
        # 重复检测 = 同一物理检测被输出多次 -> 同簇共享一次噪声实现
        clusters = (
            scenario.duplicate_cluster_sizes[frame_id]
            if scenario.duplicate_cluster_sizes is not None
            else [1] * len(dets)
        )
        raw = []
        idx = 0
        for cluster_size in clusters:
            if cluster_size == 0:
                continue
            if measurement_std > 0:
                jitter = rng.normal(0.0, measurement_std, size=2)
            for _ in range(cluster_size):
                x, y, did = dets[idx]
                if measurement_std > 0:
                    x = float(x) + float(jitter[0])
                    y = float(y) + float(jitter[1])
                raw.append((float(x), float(y), did))
                idx += 1
        results.append(tracker.step(frame_id, float(ts), raw))

    metrics = _evaluate(scenario, results)
    return metrics, results


def _evaluate(scenario: Scenario, results: list) -> Metrics:
    """离线打分：逐帧把可见真值对象绑定到已确认/滑行轨迹。"""
    id_switches, errors = _switch_and_errors(scenario, results)

    used_tracks = {
        t.track_id
        for r in results
        for t in r.tracks
        if t.state in ("confirmed", "coasting")
    }
    births_total = sum(len(r.births) for r in results)
    deleted_total = sum(len(r.deleted) for r in results)

    err = np.asarray(errors, dtype=np.float64)
    err = err if err.size else np.array([0.0])
    final = results[-1]
    return Metrics(
        scenario=scenario.name,
        id_switches=id_switches,
        rmse=float(np.sqrt((err**2).mean())),
        mean_error=float(err.mean()),
        max_error=float(err.max()),
        matched_frames=int(err.size),
        confirmed_ids_used=sorted(used_tracks),
        final_alive_tracks=sum(1 for t in final.tracks if t.state != "deleted"),
        births_total=births_total,
        deleted_total=deleted_total,
    )


def _switch_and_errors(scenario: Scenario, results: list) -> tuple[int, list[float]]:
    """构建每条真值随时间的轨迹绑定序列，统计身份切换次数与逐帧误差。

    绑定策略：每帧在已确认/滑行轨迹中，未被其他真值占用者里选欧氏最近；
    已绑定轨迹仍存活则锁定。真值不可见帧不参与。
    """
    binding: dict[str, int] = {}
    previous: dict[str, int] = {}
    id_switches = 0
    errors: list[float] = []

    for k, r in enumerate(results):
        confirmed = {
            t.track_id: np.array(t.position, dtype=np.float64)
            for t in r.tracks
            if t.state in ("confirmed", "coasting")
        }
        # 身份随轨迹存活延续；消失则解除（重绑新 ID 计一次切换）
        for oid, tid in list(binding.items()):
            if tid not in confirmed:
                binding.pop(oid)

        # 按真值顺序确定性处理
        for obj in scenario.objects:
            z = obj.xy[k]
            if np.any(np.isnan(z)):
                continue
            z = np.asarray(z, dtype=np.float64)
            cur = binding.get(obj.truth_id)
            if cur is None:
                free = [
                    (float(np.linalg.norm(pos - z)), cand)
                    for cand, pos in confirmed.items()
                    if cand not in binding.values()
                ]
                if free:
                    _, cur = min(free, key=lambda f: f[0])
                    binding[obj.truth_id] = cur
                    prev = previous.get(obj.truth_id)
                    if prev is not None and prev != cur:
                        id_switches += 1
            if cur is not None:
                errors.append(float(np.linalg.norm(confirmed[cur] - z)))
                previous[obj.truth_id] = cur
    return id_switches, errors
