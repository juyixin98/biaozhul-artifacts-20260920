"""生成示例输入文件（examples/）。

这些文件只包含可直接 POST 的协议数据（检测点坐标与检测端标签），
不包含任何真值轨迹 ID——真值只存在于 app/evaluation.py 的离线夹具中。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"


def write_json(path: Path, obj) -> None:
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    EX.mkdir(exist_ok=True)

    # 1) 交叉运动：两目标错时交叉（A: (t,t)，B: (8+t,12-t)）
    frames = []
    for k in range(21):
        frames.append(
            {
                "frame_id": k,
                "timestamp": float(k),
                "detections": [
                    {"x": float(k), "y": float(k), "detection_id": f"A-{k}"},
                    {
                        "x": float(8 + k),
                        "y": float(12 - k),
                        "detection_id": f"B-{k}",
                    },
                ],
            }
        )
    write_json(EX / "frames_crossing.json", frames)

    # 2) 短遮挡 + 空帧：第 10..13 帧为空
    frames = []
    for k in range(20):
        dets = (
            []
            if 10 <= k < 14
            else [
                {
                    "x": float(0.8 * k),
                    "y": 5.0,
                    "detection_id": f"A-{k}",
                }
            ]
        )
        frames.append(
            {"frame_id": k, "timestamp": float(k), "detections": dets}
        )
    write_json(EX / "frames_occlusion.json", frames)

    # 3) 重复检测：每帧同一目标两个很近的点
    frames = []
    for k in range(12):
        frames.append(
            {
                "frame_id": k,
                "timestamp": float(k),
                "detections": [
                    {
                        "x": float(0.7 * k),
                        "y": float(1.0 + 0.2 * k),
                        "detection_id": f"A-{k}-a",
                    },
                    {
                        "x": float(0.7 * k + 0.04),
                        "y": float(1.0 + 0.2 * k - 0.03),
                        "detection_id": f"A-{k}-b",
                    },
                ],
            }
        )
    write_json(EX / "frames_duplicates.json", frames)

    # 4) 变时间间隔：dt 不相等，验证 dt 真实参与状态转移
    rng = np.random.default_rng(1)
    frames = []
    ts = 0.0
    for k in range(12):
        frames.append(
            {
                "frame_id": k,
                "timestamp": round(float(ts), 3),
                "detections": [
                    {
                        "x": float(0.8 * ts),
                        "y": 0.0,
                        "detection_id": f"A-{k}",
                    }
                ],
            }
        )
        ts += float(rng.uniform(0.5, 2.0))
    write_json(EX / "frames_variable_dt.json", frames)

    write_json(
        EX / "create_session.json",
        {
            "session_id": "demo",
            "config": {
                "confirm_hits": 3,
                "max_misses": 5,
                "gate_threshold": 9.210,
                "merge_radius": 0.25,
            },
        },
    )
    print(f"示例文件已写入 {EX}")


if __name__ == "__main__":
    main()
