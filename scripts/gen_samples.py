#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成两个请求样本：
  samples/skew3.json —— 3 张表带倾斜数据，展示“均匀假设估计行数 vs 实际行数”的差异
  samples/star8.json —— 8 张表星型连接（仅统计），展示 DP 在表数上限下的选序

用法：python3 scripts/gen_samples.py
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")


def make_skew3(n=100, hot_share=0.90, hot_key=1):
    """
    三张表 t1/t2/t3 各 n 行，连接键 k 有 m=n/10 个不同值，
    其中 hot_key 占 90% 的行（倾斜），其余 9 个键各占 10%/9。
    均匀假设按 NDV 估计选择率 1/m，严重低估热点键产生的连接行数。
    """
    m = n // 10
    other = n - int(n * hot_share)          # 非热点键总行数
    per_other = other // (m - 1)            # 每个非热点键的行数

    def rows():
        data = [hot_key] * (n - per_other * (m - 1))
        for k in range(2, m + 1):
            data += [k] * per_other
        assert len(data) == n
        return [[v] for v in data]

    def ndv_and_counts(data):
        counts = {}
        for (v,) in data:
            counts[v] = counts.get(v, 0) + 1
        return len(counts), counts

    r1, r2, r3 = rows(), rows(), rows()
    ndv, c1 = ndv_and_counts(r1)
    _, c2 = ndv_and_counts(r2)
    _, c3 = ndv_and_counts(r3)

    # 实际两表连接行数 = Σ count1(k)*count2(k)
    actual_2way = sum(c1[k] * c2[k] for k in c1)
    actual_3way = sum(c1[k] * c2[k] * c3[k] for k in c1)
    est_2way = n * n // ndv
    est_3way = n ** 3 // ndv ** 2

    req = {
        "comment": (
            "倾斜统计实验：t1/t2/t3 各 %d 行，键 k 有 %d 个不同值，"
            "键 %d 占 %.0f%% 的行。均匀假设估计两表连接=%d、三表=%d；"
            "实际两表=%d、三表=%d（见响应 estimateVsActual）。"
        ) % (n, ndv, hot_key, hot_share * 100, est_2way, est_3way,
             actual_2way, actual_3way),
        "tables": [
            {"name": "t1", "columns": ["k"], "rows": r1},
            {"name": "t2", "columns": ["k"], "rows": r2},
            {"name": "t3", "columns": ["k"], "rows": r3},
        ],
        "joins": [
            ["t1", "k", "t2", "k"],
            ["t2", "k", "t3", "k"],
        ],
        "options": {"execute": True, "enumerate": True, "resultLimit": 3},
    }

    print("skew3: n=%d ndv=%d hotKey=%d hotRows/table=%d"
          % (n, ndv, hot_key, c1[hot_key]))
    print("  2-way  est=%d actual=%d  ratio=%.2f"
          % (est_2way, actual_2way, actual_2way / est_2way))
    print("  3-way  est=%d actual=%d  ratio=%.2f"
          % (est_3way, actual_3way, actual_3way / est_3way))
    return req


def make_star8():
    """
    8 表星型：大事实表 f（100 万行）+ 7 个维度表 d1..d7（主键 1..|di|）。
    仅统计，不实际执行。演示 DP 在 8 表（255 个子集）下的枚举与选序。
    """
    dim_sizes = [10, 50, 100, 500, 1000, 5000, 10000]
    fact_rows = 1_000_000
    tables = [{
        "name": "f",
        "columns": ["id"] + ["d%d_id" % i for i in range(1, 8)],
        "rowCount": fact_rows,
        "ndv": {"id": fact_rows, **{
            "d%d_id" % i: dim_sizes[i - 1] for i in range(1, 8)
        }},
    }]
    joins = []
    for i, size in enumerate(dim_sizes, start=1):
        tables.append({
            "name": "d%d" % i,
            "columns": ["id"],
            "rowCount": size,
            "ndv": {"id": size},
        })
        joins.append(["f", "d%d_id" % i, "d%d" % i, "id"])

    return {
        "comment": "8 表星型连接（上限）：事实表 f(1,000,000) + 7 个维度表，仅统计",
        "tables": tables,
        "joins": joins,
        "options": {"execute": False, "enumerate": False, "resultLimit": 0},
    }


def make_uniform3(n=100):
    """
    倾斜实验的对照组：与 skew3 完全相同的行数(100)和 NDV(10)，
    但 10 个键严格均匀（每键 10 行）。估计同样是两表 1000、三表 10000，
    实际恰好等于估计 —— 说明失准来自数据倾斜，而非估计公式或执行器错误。
    """
    data = [[(i % 10) + 1] for i in range(n)]
    req = {
        "comment": (
            "均匀对照组：t1/t2/t3 各 %d 行，键 k 有 10 个不同值且严格均匀（每键 10 行）。"
            "估计两表=1000、三表=10000，实际相同（见 estimateVsActual，差异为 0）。"
        ) % n,
        "tables": [
            {"name": "t1", "columns": ["k"], "rows": [list(r) for r in data]},
            {"name": "t2", "columns": ["k"], "rows": [list(r) for r in data]},
            {"name": "t3", "columns": ["k"], "rows": [list(r) for r in data]},
        ],
        "joins": [
            ["t1", "k", "t2", "k"],
            ["t2", "k", "t3", "k"],
        ],
        "options": {"execute": True, "enumerate": True, "resultLimit": 3},
    }
    print("uniform3: n=%d 2-way est=actual=1000 3-way est=actual=10000" % n)
    return req


def main():
    os.makedirs(SAMPLES, exist_ok=True)
    skew = make_skew3()
    with open(os.path.join(SAMPLES, "skew3.json"), "w", encoding="utf-8") as f:
        json.dump(skew, f, ensure_ascii=False, indent=2)
        f.write("\n")
    uniform = make_uniform3()
    with open(os.path.join(SAMPLES, "uniform3.json"), "w", encoding="utf-8") as f:
        json.dump(uniform, f, ensure_ascii=False, indent=2)
        f.write("\n")
    star = make_star8()
    with open(os.path.join(SAMPLES, "star8.json"), "w", encoding="utf-8") as f:
        json.dump(star, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("wrote samples/skew3.json, samples/uniform3.json and samples/star8.json")


if __name__ == "__main__":
    main()
