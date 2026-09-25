"""命令行演示入口。

用法::

    python -m pitjoin.cli demo      # 规范场景：逐事件打印选择依据（可手算对照）
    python -m pitjoin.cli leakage   # 防泄漏 vs 朴素 as-of 的模型对照
    python -m pitjoin.cli serve     # 启动本地 HTTP 服务（默认 127.0.0.1:8000）
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

import numpy as np

from .engine import JoinConfig, pit_join
from .model import accuracy, chronological_split, train_logistic
from .serialize import frame_to_dict
from .synthetic import build_canonical_dataset, build_generated_dataset
from .times import to_iso

# 事件标签（与 build_canonical_dataset 的事件顺序一致）
EVENT_LABELS: tuple[str, ...] = (
    "E1 cust_A @T0+12h",
    "E2 cust_A @T0+1d12h",
    "E3 cust_A @T0+3d12h",
    "E4 cust_B @T0+1d",
    "E5 cust_C @T0+1d",
    "E6 cust_C @T0+11d",
    "E7 cust_D @T0+1d",
    "E8 cust_D @T0+6d",
    "E9 cust_E @T0+1d",
)

# 规范场景的手算期望：(事件序号, 事件标签, 特征, 期望原因, 期望值)
EXPECTED: tuple[tuple[int, str, str, str, Any], ...] = (
    (0, "E1 cust_A @T0+12h", "f_balance", "SELECTED", 100.0),
    (1, "E2 cust_A @T0+1d12h", "f_balance", "SELECTED", 110.0),   # rA3 尚未入库
    (2, "E3 cust_A @T0+3d12h", "f_balance", "SELECTED", 111.0),   # rA3 已入库
    (3, "E4 cust_B @T0+1d", "f_score", "SELECTED", 3.0),          # 版本号裁决
    (3, "E4 cust_B @T0+1d", "f_tier", "SELECTED", 20.0),          # 入库时间裁决
    (4, "E5 cust_C @T0+1d", "f_balance", "ALL_FUTURE", None),
    (5, "E6 cust_C @T0+11d", "f_balance", "SELECTED", 500.0),
    (6, "E7 cust_D @T0+1d", "f_balance", "ALL_LATE", None),
    (7, "E8 cust_D @T0+6d", "f_balance", "SELECTED", 77.0),
    (8, "E9 cust_E @T0+1d", "f_balance", "MISSING_KEY", None),
)


def run_demo() -> int:
    ds = build_canonical_dataset()
    print("=" * 78)
    print("规范场景：离线特征时间点连接（T0 = 2026-01-01T00:00Z）")
    print("=" * 78)
    print("特征记录：")
    for r in ds.records:
        print(
            f"  {r.record_id:>4} entity={r.entity_id} {r.feature_name:<9} "
            f"value={r.value:<6.1f} v{r.version} effective={to_iso(r.effective_time)} "
            f"ingest={to_iso(r.ingest_time)}"
        )

    frame = pit_join(ds.events, ds.store, JoinConfig(use_event_as_of=True))
    print("\n逐事件选择结果与依据：")
    feature_index = {f: i for i, f in enumerate(frame.feature_names)}

    n_events = len(ds.events)

    def ev_at(i: int, fname: str):
        # 证据按特征分块排列：feature_index * N + event_index
        return frame.evidence[feature_index[fname] * n_events + i]

    failures = 0
    for i, event in enumerate(ds.events):
        print(f"\n事件 {EVENT_LABELS[i]}  event_time={to_iso(event.event_time)}")
        for fname in frame.feature_names:
            ev = ev_at(i, fname)
            # 只输出与该实体相关的特征依据，避免无关实体的缺特征噪声
            if ev.reason == "MISSING_KEY" and ev.entity_id not in ("cust_E",):
                continue
            print("   " + ev.explain())

    print("\n" + "=" * 78)
    print("手算对照（期望 vs 实际）：")
    print("=" * 78)
    for event_idx, label, fname, exp_reason, exp_value in EXPECTED:
        ev = ev_at(event_idx, fname)
        ok_reason = ev.reason == exp_reason
        ok_value = (
            exp_value is None
            or (ev.selected_value is not None and np.isclose(ev.selected_value, exp_value))
        )
        ok = ok_reason and ok_value
        failures += 0 if ok else 1
        mark = "PASS" if ok else "FAIL"
        got = "None" if ev.selected_value is None else f"{ev.selected_value:.1f}"
        want = "None" if exp_value is None else f"{exp_value:.1f}"
        print(
            f"  [{mark}] {label:<22} {fname:<9} "
            f"reason 期望={exp_reason:<11} 实际={ev.reason:<11} "
            f"value 期望={want:<5} 实际={got:<5}"
        )

    print("\n原因码统计：", dict(frame.summary()))
    print(f"\n手算对照结果：{len(EXPECTED) - failures}/{len(EXPECTED)} 通过")
    return 1 if failures else 0


def run_leakage() -> int:
    print("=" * 78)
    print("泄漏对照实验：防泄漏 PIT join vs 忽略入库时间的朴素 as-of join")
    print("=" * 78)
    ds = build_generated_dataset()
    print(
        f"合成数据：{ds.preliminary_records} 条准时初步值(v1, 带噪声) + "
        f"{ds.revision_records} 条晚到定稿值(v2, 72h 后入库)，{len(ds.events)} 个事件"
    )

    results: dict[str, Any] = {}
    for name, use_asof in (("防泄漏 PIT", True), ("朴素 as-of(会泄漏)", False)):
        frame = pit_join(ds.events, ds.store, JoinConfig(use_event_as_of=use_asof))
        split = chronological_split(frame, train_ratio=0.7)
        model = train_logistic(split.x_train, split.y_train)
        train_acc = accuracy(split.y_train, model.predict(split.x_train))
        test_acc = accuracy(split.y_test, model.predict(split.x_test))
        nan_ratio = float(np.isnan(split.x_train).mean())
        results[name] = (train_acc, test_acc, nan_ratio)
        print(
            f"\n[{name}] 训练集 NaN 占比={nan_ratio:6.2%}  "
            f"训练准确率={train_acc:6.3f}  时间外测试准确率={test_acc:6.3f}"
        )

    pit_test = results["防泄漏 PIT"][1]
    naive_test = results["朴素 as-of(会泄漏)"][1]
    gap = naive_test - pit_test
    print("\n解读：")
    print("  朴素 join 让事件时刻看到了 72h 后才入库的'定稿值'（=标签所用真值），")
    print(f"  离线时间外准确率虚高约 {gap:+.3f}；防泄漏 PIT 只使用当时真正可见的带噪初步值，")
    print("  这部分'准确率'在真实上线时不可复现 —— 即事后修订泄漏。")
    return 0


def run_serve(port: int) -> int:
    from .service import serve

    serve(port=port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pitjoin", description="特征时间点连接基础设施")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="规范场景手算对照")
    sub.add_parser("leakage", help="泄漏对照实验")
    serve_p = sub.add_parser("serve", help="启动本地 HTTP 服务")
    serve_p.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.cmd == "demo":
        return run_demo()
    if args.cmd == "leakage":
        return run_leakage()
    return run_serve(args.port)


if __name__ == "__main__":
    sys.exit(main())
