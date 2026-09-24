"""生成示例输入（JSON）与对应人工真值，写到 examples/。

用法：
    PYTHONPATH=src python3 scripts/generate_examples.py

每个场景生成两个文件：
    examples/<scene>.request.json   —— POST /api/v1/segment 的请求体
    examples/<scene>.truth.json     —— 人工真值 {truth: [0/1, ...], meta: {...}}
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from groundseg import GroundSegParams, evaluate, segment_points  # noqa: E402
from groundseg import scenes  # noqa: E402

EXAMPLE_SCENES = [
    "flat_ground",
    "sloped_ground",
    "steep_slope",
    "ground_with_wall",
    "sparse_ground",
    "diffuse_noise",
    "ground_with_noise",
    "duplicate_points",
]

PARAMS_OVERRIDE: dict[str, dict] = {
    # 陡坡场景另附一份放宽倾角的请求，演示参数旋钮
}


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "examples")
    os.makedirs(out_dir, exist_ok=True)

    params = GroundSegParams()
    summary = []
    for name in EXAMPLE_SCENES:
        pts, truth, meta = scenes.SCENES[name]()
        request_body = {"points": pts.tolist()}
        req_path = os.path.join(out_dir, f"{name}.request.json")
        truth_path = os.path.join(out_dir, f"{name}.truth.json")
        with open(req_path, "w", encoding="utf-8") as f:
            json.dump(request_body, f, ensure_ascii=False)
        with open(truth_path, "w", encoding="utf-8") as f:
            json.dump({"truth": truth.astype(int).tolist(), "meta": meta},
                      f, ensure_ascii=False)

        res = segment_points(pts, params)
        m = evaluate(res.labels, truth.tolist())["ground"]
        summary.append((name, pts.shape[0], m, res.stats))
        print(f"generated {name:20s} N={pts.shape[0]:5d}  "
              f"precision={m['precision']:.3f} recall={m['recall']:.3f} "
              f"undecided_rate={m['undecided_rate']:.3f}")

    # 陡坡场景附加一个放宽倾角的请求
    pts, _, _ = scenes.SCENES["steep_slope"]()
    with open(os.path.join(out_dir, "steep_slope.loose.request.json"),
              "w", encoding="utf-8") as f:
        json.dump({"points": pts.tolist(),
                   "params": {"max_tilt_deg": 40.0, "min_inlier_count": 10}},
                  f, ensure_ascii=False)
    print("\n示例文件已写入", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
