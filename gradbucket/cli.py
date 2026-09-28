"""命令行演示：跑确定性场景，打印桶世代与归约依据。

用法::

    python -m gradbucket.cli demo-happy
    python -m gradbucket.cli demo-unequal
    python -m gradbucket.cli demo-omit
    python -m gradbucket.cli demo-lost
    python -m gradbucket.cli demo-pending
    python -m gradbucket.cli demo-all
    python -m gradbucket.cli cluster --workers 3 --samples 12   # 真实多进程
"""

from __future__ import annotations

import argparse
import json

import numpy as np

from .graph import LinearModel, ModelConfig
from .reference import union_batch_gradient
from .runtime import Scenario, representative_orders, run_scenario


def _print_header(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _print_result(name: str, res) -> bool:
    print(f"场景 {name!r} 判定: {res.verdict.value}")
    coord = res.coordinator
    print(f"提交过程中观察到的世代序列: {res.generations_seen_during_submission}")
    weights_changed = any(
        any(np.any(v != 0.0) for v in snap.values())
        for snap in res.weights_seen_during_submission
    )
    print(f"封存前权重是否被提前改动: {weights_changed}")
    if res.commit is None:
        recs = [d for d in coord.diagnostics().all() if d.verdict == res.verdict]
        if recs:
            last = recs[-1]
            print(f"理由: {last.reason}")
            print(f"关键状态: {json.dumps(last.key_state, ensure_ascii=False, sort_keys=True)}")
        return True

    c = res.commit
    print(f"世代迁移: {c.generation_before} -> {c.generation_after}")
    print("桶归约依据:")
    for basis in c.bucket_bases:
        print(json.dumps(basis, ensure_ascii=False, indent=2, sort_keys=True))
    # 逐参数参照：缺梯度的工作者从该参数的"覆盖者联合批"中剔除，
    # 这正是 reducer 逐槽加权平均的数学定义。
    params = list(c.gradients)
    covered = {
        p: [w for w in sorted(res.shards) if p not in res.omit_params.get(w, set())]
        for p in params
    }
    ok = True
    for p in params:
        model = LinearModel(ModelConfig())
        ref_grads, n_eff = union_batch_gradient(
            model,
            [(res.shards[w][0], res.shards[w][1]) for w in covered[p]],
        )
        got = c.gradients[p]
        same = np.allclose(got, ref_grads[p], rtol=0, atol=1e-12)
        ok = ok and same
        print(f"  参照核对 {p}: 覆盖者={covered[p]} 有效样本 N={n_eff} "
              f"最大绝对偏差 {np.max(np.abs(got - ref_grads[p])):.3e} "
              f"-> {'一致' if same else '不一致'}")
    return ok


SCENARIOS = {
    "demo-happy": lambda: Scenario(),
    "demo-unequal": lambda: Scenario(shard_sizes=[2, 3, 7]),
    "demo-omit": lambda: Scenario(
        shard_sizes=[2, 3, 7],
        omit_params={"w2": {"w"}},
    ),
    "demo-lost": lambda: Scenario(
        shard_sizes=[2, 3, 7],
        die_after={"w2": 0},
        silence_before_seal=6.0,
    ),
    "demo-pending": lambda: Scenario(
        shard_sizes=[2, 3, 7],
        withhold={("w2", 1)},
        silence_before_seal=1.0,
    ),
}


def _demo_all() -> bool:
    all_ok = True
    base_grads = None
    for name, factory in SCENARIOS.items():
        _print_header(name)
        res = run_scenario(factory())
        all_ok = _print_result(name, res) and all_ok
        if name == "demo-happy":
            base_grads = res.commit.gradients

    _print_header("顺序无关性交叉验证（demo-happy 的全部代表完成顺序）")
    for order in representative_orders(3, 2):
        res = run_scenario(Scenario(order=order))
        same = all(
            np.array_equal(res.commit.gradients[k], base_grads[k]) for k in base_grads
        )
        all_ok = all_ok and same
        print(f"顺序 {order} -> 逐位一致: {same}")
    return all_ok


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="gradbucket 教学演示")
    p.add_argument("scenario", choices=[*SCENARIOS, "demo-all", "cluster"])
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--samples", type=int, default=12)
    p.add_argument("--config", default="",
                   help="JSON 配置路径；给出时集群按该配置启动（如 configs/ 下）")
    p.add_argument("--dump-diag", default="")
    args = p.parse_args(argv)

    if args.scenario == "cluster":
        from .cluster import run_cluster
        kwargs = {}
        if args.config:
            from .config import load_config, cluster_kwargs
            kwargs = cluster_kwargs(load_config(args.config))
        else:
            kwargs = {"n_workers": args.workers, "n_samples": args.samples}
        result = run_cluster(**kwargs)
        print(json.dumps(result["seal"], ensure_ascii=False, indent=2,
                         sort_keys=True))
        print("工作者退出码:", result["worker_return_codes"])
        print("日志已写入 logs/cluster.log")
        return 0

    if args.scenario == "demo-all":
        ok = _demo_all()
    else:
        _print_header(args.scenario)
        res = run_scenario(SCENARIOS[args.scenario]())
        ok = _print_result(args.scenario, res)
        if args.dump_diag:
            res.coordinator.diagnostics().dump_jsonl(args.dump_diag)
    print("\n总体判断:", "通过" if ok else "存在不一致")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
