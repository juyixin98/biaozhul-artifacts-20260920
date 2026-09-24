"""命令行入口。

* qosdiag-serve   : 启动 FastAPI + rclpy 采集器（真实 DDS 发现）
* qosdiag-analyze : 离线分析示例拓扑 JSON（规则引擎不需要 ROS，便于 CI 演示）
* qosdiag-snapshot: 采集一次实时拓扑，保存受完整性保护的快照
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import uvicorn

from .models import Topology
from .rules import RULES_VERSION, diagnose_topology
from .store import SnapshotStore


def _print_diagnoses(diagnoses: list) -> None:
    if not diagnoses:
        print("（当前未发现任何端点）")
        return
    for d in diagnoses:
        print(f"话题 {d.topic}  [{d.topic_type}]  =>  {d.verdict.upper()}"
              f"  (pub={d.publisher_count}, sub={d.subscription_count})")
        if d.verdict == "insufficient_evidence":
            print("    端点未凑齐，证据不足 —— 不判定为故障（短暂未发现不等于永久故障）。")
        for pair in d.pairs:
            pub = f"/{pair.publisher.node_namespace.strip('/')}/{pair.publisher.node_name}".replace("//", "/")
            sub = f"/{pair.subscription.node_namespace.strip('/')}/{pair.subscription.node_name}".replace("//", "/")
            print(f"    配对 {pub}(P) <-> {sub}(S): {pair.verdict.upper()}")
            for f in pair.findings:
                mark = {"incompatible": "  [不兼容]", "risk": "  [风险]  ", "ok": "  [通过]  "}
                print(f"    {mark[f.severity]} {f.rule_id} {f.title}: {f.message}")
                c = f.chain
                if c.publisher_value is not None or c.subscription_value is not None:
                    print(f"           链路: publisher={c.publisher_value} "
                          f"subscription={c.subscription_value} 期望={c.expected}")
        for up in d.unmatched_publishers:
            print(f"    [未配对发布方] {up.node_name} state={up.state}")
        for us in d.unmatched_subscriptions:
            print(f"    [未配对订阅方] {us.node_name} state={us.state}")


def cmd_analyze(args: argparse.Namespace) -> int:
    data = json.loads(open(args.file, encoding="utf-8").read())
    topology = Topology.model_validate(data)
    diagnoses = diagnose_topology(topology)
    print(f"规则版本: {RULES_VERSION}")
    _print_diagnoses(diagnoses)
    return 0 if not any(d.verdict == "incompatible" for d in diagnoses) else 2


def cmd_snapshot(args: argparse.Namespace) -> int:
    from .collector import QoSTopologyCollector

    collector = QoSTopologyCollector(
        discovery_interval=args.interval, missing_grace_seconds=args.grace
    )
    collector.start()
    try:
        print(f"采集真实 DDS 端点中（等待 {args.settle}s 让发现收敛）...")
        time.sleep(args.settle)
        topology = collector.get_topology()
        store = SnapshotStore(args.data_dir)
        snapshot = store.create_snapshot(topology)
        path = store.save(snapshot)
        print(f"已保存快照: {path}")
        print(f"规则版本:   {snapshot.rules_version}")
        print(f"端点数:     {len(topology.endpoints)}，事件数: {len(topology.events)}")
        _print_diagnoses(snapshot.diagnoses)
    finally:
        collector.shutdown()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .collector import QoSTopologyCollector
    from .service import create_app

    collector = QoSTopologyCollector(
        discovery_interval=args.interval, missing_grace_seconds=args.grace
    )
    collector.start()
    app = create_app(collector=collector, data_dir=args.data_dir)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        collector.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qosdiag", description="ROS 2 QoS 兼容诊断服务")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务 + 实时端点采集")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--data-dir", default="data")
    p_serve.add_argument("--interval", type=float, default=1.0, help="DDS 发现轮询间隔(秒)")
    p_serve.add_argument("--grace", type=float, default=5.0, help="端点消失宽限期(秒)")
    p_serve.set_defaults(func=cmd_serve)

    p_snap = sub.add_parser("snapshot", help="采集一次实时拓扑并保存快照")
    p_snap.add_argument("--data-dir", default="data")
    p_snap.add_argument("--interval", type=float, default=1.0)
    p_snap.add_argument("--grace", type=float, default=5.0)
    p_snap.add_argument("--settle", type=float, default=5.0, help="启动后等待发现收敛的秒数")
    p_snap.set_defaults(func=cmd_snapshot)

    p_an = sub.add_parser("analyze", help="离线分析示例拓扑 JSON（无需 ROS）")
    p_an.add_argument("file", help="示例拓扑 JSON 路径")
    p_an.set_defaults(func=cmd_analyze)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
