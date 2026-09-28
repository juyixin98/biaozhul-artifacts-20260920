"""工作者进程入口（真实多进程路径）。

流程（全部通过 HTTP，与 coordinator 不在同一进程）：
1. GET /rounds/current 取固定桶布局与槽位描述；
2. GET /weights 取本轮权重快照（封存前世代不变）；
3. 在本地合成数据的确定性分片上算梯度；
4. 逐桶 POST 提交；被 ``--omit`` 的参数写显式占位（零 + mask False）；
5. ``--fail-after-bucket`` 可让进程在提交指定桶后立即退出——模拟节点中断；
6. ``--withhold-bucket`` 压下某桶不提交但保持心跳——模拟"无法判定"。
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx
import numpy as np

from .graph import LinearModel, ModelConfig, make_dataset, shard_indices


def _retry(call, *, attempts: int = 40, delay: float = 0.1):
    last = None
    for _ in range(attempts):
        try:
            return call()
        except httpx.HTTPError as exc:  # 等主节点起来
            last = exc
            time.sleep(delay)
    raise RuntimeError(f"服务不可达: {last}")


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="gradbucket 工作者")
    p.add_argument("--base-url", default="http://127.0.0.1:8765")
    p.add_argument("--worker-id", required=True)
    p.add_argument("--rank", type=int, required=True)
    p.add_argument("--n-workers", type=int, required=True)
    p.add_argument("--n-samples", type=int, default=12)
    p.add_argument("--shard-sizes", default="",
                   help="逗号分隔，如 1,2,1；留空为均匀分片")
    p.add_argument("--seed", type=int, default=445)
    p.add_argument("--omit", default="",
                   help="逗号分隔的不计算参数名（提交显式占位），如 spare")
    p.add_argument("--fail-after-bucket", type=int, default=-1,
                   help="提交完该桶后进程立即退出（模拟节点中断）")
    p.add_argument("--withhold-bucket", type=int, default=-1,
                   help="压下该桶不提交（但保持心跳）")
    p.add_argument("--linger-seconds", type=float, default=3.0,
                   help="提交完成后继续续约心跳的时长，供主节点判活")
    args = p.parse_args(argv)

    base_url = args.base_url
    client = httpx.Client(base_url=base_url, timeout=5.0)

    view = _retry(lambda: client.get("/rounds/current")).json()
    weights_msg = client.get("/weights").json()
    generation = weights_msg["generation"]
    round_index = view["round_index"]
    bucket_sizes = view["bucket_sizes"]
    slots = view["slots"]

    # 由槽位描述反推参数形状（与服务端同一份固定布局）。
    n_in = next(s for s in slots if s["param"] == "w")["shape"][1]
    n_out = next(s for s in slots if s["param"] == "w")["shape"][0]
    has_spare = any(s["param"] == "spare" for s in slots)
    cfg = ModelConfig(n_in=n_in, n_out=n_out, include_spare=has_spare)
    model = LinearModel(cfg)
    model.set_weights(
        np.asarray(weights_msg["weights"]["w"], dtype=np.float64),
        np.asarray(weights_msg["weights"]["b"], dtype=np.float64),
    )

    shard_sizes = (
        [int(x) for x in args.shard_sizes.split(",")] if args.shard_sizes else None
    )
    idx = shard_indices(
        args.n_samples, args.rank, args.n_workers,
        shard_sizes=shard_sizes, seed=args.seed,
    )
    x_all, y_all = make_dataset(
        args.n_samples, seed=args.seed, n_in=n_in, n_out=n_out
    )
    x, y = x_all[idx], y_all[idx]
    if x.shape[0] == 0:
        print(f"[{args.worker_id}] 分片为空，退出", file=sys.stderr)
        return 2
    grads, sample_count = model.gradients(x, y)
    omit = {s for s in args.omit.split(",") if s}

    # 槽位按全局 offset 排序，按 bucket_sizes 切段。
    ordered = sorted(slots, key=lambda s: s["offset"])
    boundaries = np.cumsum([0] + list(bucket_sizes)).tolist()
    for b in range(len(bucket_sizes)):
        if b == args.withhold_bucket:
            print(f"[{args.worker_id}] 压下桶 {b} 不提交（保持心跳）")
            continue
        bslots = ordered[boundaries[b] : boundaries[b + 1]]
        vec_parts, mask_parts = [], []
        for s in bslots:
            name = s["param"]
            if name in omit or name not in grads:
                vec_parts.append(np.zeros(s["length"], dtype=np.float64))
                mask_parts.append(np.zeros(s["length"], dtype=bool))
            else:
                vec_parts.append(grads[name].reshape(-1).astype(np.float64))
                mask_parts.append(np.ones(s["length"], dtype=bool))
        vec = np.concatenate(vec_parts)
        mask = np.concatenate(mask_parts)
        resp = client.post(
            f"/rounds/{round_index}/buckets/{b}",
            json={
                "generation": generation,
                "worker_id": args.worker_id,
                "vec": vec.tolist(),
                "mask": mask.tolist(),
                "sample_count": int(sample_count),
            },
        )
        resp.raise_for_status()
        body = resp.json()
        verdict = body["verdict"]
        print(f"[{args.worker_id}] 桶 {b} -> {verdict} "
              f"(n={sample_count}, 省略参数={sorted(omit) or '无'})")
        if verdict != "ACCEPTED":
            # 服务端拒绝（陈旧世代/重复/形状不符/轮已终结等）是显式失败，
            # 绝不能打印后以退出码 0 假装成功。
            print(f"[{args.worker_id}] 提交被拒绝: {verdict}，退出码 4",
                  file=sys.stderr)
            return 4
        if b == args.fail_after_bucket:
            print(f"[{args.worker_id}] 模拟节点中断，立即退出", file=sys.stderr)
            return 3

    # 收尾心跳：轮次仍 OPEN 时持续续约；一旦封存/拒绝即正常退出。
    deadline = time.time() + args.linger_seconds
    while time.time() < deadline:
        client.post(f"/workers/{args.worker_id}/heartbeat")
        time.sleep(0.05)
        try:
            view = client.get("/rounds/current")
            if view.status_code == 200 and view.json().get("status") != "OPEN":
                break
        except httpx.HTTPError:
            break
    print(f"[{args.worker_id}] 完成，样本数={sample_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
