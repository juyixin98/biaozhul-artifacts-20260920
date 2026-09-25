"""端到端演示：对四个验收场景运行规划与双执行器对比。

用法：``PYTHONPATH=src python3 scripts/run_demo.py``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tensor_planner.api import handle_request  # noqa: E402

REQUEST_DIR = ROOT / "examples" / "requests"


def main() -> int:
    request_files = sorted(REQUEST_DIR.glob("*.json"))
    print(f"共 {len(request_files)} 个场景\n")
    all_passed = True
    for path in request_files:
        spec = json.loads(path.read_text(encoding="utf-8"))
        report = handle_request(spec)
        m = report["memory"]
        all_passed &= report["passed"]
        print(f"[{path.name}]")
        print(f"  图输出: {report['outputs']}")
        print(
            "  arena: {n} 元素 ({b} 字节); 峰值存活 {p} 元素".format(
                n=m["arena_elements"],
                b=m["arena_bytes"],
                p=m["peak_live_elements"],
            )
        )
        print(
            "  累计分配 基线={na} 复用={re} 节省 {save} ({ratio:.1%})".format(
                na=m["naive_total_allocated_elements"],
                re=m["reuse_total_allocated_elements"],
                save=m["saved_total_elements"],
                ratio=m["saved_total_ratio"],
            )
        )
        print(
            "  峰值存活 基线={np} 复用={rp}; 与基线最大绝对误差={d}".format(
                np=m["naive_no_reuse_peak_elements"],
                rp=m["reuse_peak_elements"],
                d=report["correctness"]["max_abs_diff_vs_naive"],
            )
        )
        chains = ", ".join(
            " -> ".join(c["sequence"]) for c in report["reuse_chains"]
        )
        print(f"  槽位复用链: {chains or '（无）'}")
        print(f"  预算内: {m['within_budget']}; passed={report['passed']}\n")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
