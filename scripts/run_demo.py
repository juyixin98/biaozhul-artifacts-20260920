#!/usr/bin/env python3
"""端到端复现脚本：摄取夹具 → 正常查询 → 交叉核验 → 异常用例。

运行：
    PYTHONPATH=src python3 scripts/run_demo.py

产物：
    results/demo_results.json   机器可读、按 run_id 关联的完整结果
    results/demo_results.txt    人类可读摘要（含正常与异常判定）

所有数据来自本地合成夹具（fixtures/corpora/*.json），无外部账号/业务数据。
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wfst import __version__  # noqa: E402
from wfst.algorithms.compose import compose  # noqa: E402
from wfst.algorithms.shortest_paths import nbest_paths  # noqa: E402
from wfst.builders import chain_acceptor, edit_cascade_fst  # noqa: E402
from wfst.config import Settings  # noqa: E402
from wfst.core.fst import EPSILON, NegativeCycleError  # noqa: E402
from wfst.index.store import Store  # noqa: E402
from wfst.manager import ModelManager  # noqa: E402
from wfst.query import (  # noqa: E402
    ErrorCategory,
    QueryError,
    render_output,
    run_query,
    transduce_stagewise,
)
from wfst.service.logging import RunLogger, new_run_id  # noqa: E402

FIX = ROOT / "fixtures" / "corpora"
RESULTS = ROOT / "results"

NORMAL_CASES = [
    ("char_morph_demo", "kat"),
    ("char_morph_demo", "kats"),
    ("char_morph_demo", "citi"),
    ("char_morph_demo", "doog"),
    ("char_morph_demo", "stpo"),
    ("epsilon_ambiguity_demo", "ab"),
    ("epsilon_ambiguity_demo", "b"),
    ("epsilon_ambiguity_demo", "bc"),
    ("word_morph_demo", "go"),
    ("word_morph_demo", "walk slowly"),
]

# 异常/边界用例：(语料, 输入, 期望类别)
ABNORMAL_CASES = [
    ("char_morph_demo", "", ErrorCategory.EMPTY_INPUT),
    ("char_morph_demo", "qvx", ErrorCategory.UNKNOWN_SYMBOL),
    # z 在字母表中（bagz->bags 学来），但单独的 "z" 无法归约到任何词典键：
    # 这是「符号已知但无接受路径」，与字母表外符号是不同的失败类别。
    ("char_morph_demo", "z", ErrorCategory.UNACCEPTABLE_INPUT),
]


def main() -> int:
    RESULTS.mkdir(exist_ok=True)
    settings = Settings(
        db_path=RESULTS / "demo.db",
        default_k=8,
        default_budget=200_000,
        max_input_len=64,
        smoothing_count=0.5,
        log_dir=RESULTS,
    )
    store = Store(settings.db_path)
    mgr = ModelManager(store, settings)
    logger = RunLogger(settings.log_dir)

    ingested = {}
    for path in sorted(FIX.glob("*.json")):
        result = mgr.ingest_file(path)
        ingested[result.model.corpus_id] = (result.fingerprint, result.model)

    report = {
        "run_id": new_run_id(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "service_version": __version__,
        "python": sys.version.split()[0],
        "models": {
            cid: {"fingerprint": fp, "describe": m.describe()}
            for cid, (fp, m) in ingested.items()
        },
        "normal": [],
        "cross_checks": [],
        "abnormal": [],
        "core_edge_cases": [],
    }

    failures = 0

    # ---- 正常查询 ----
    for cid, text in NORMAL_CASES:
        model = ingested[cid][1]
        run_id = new_run_id()
        try:
            resp = run_query(model, text, k=8, budget=200_000)
            payload = {
                "run_id": run_id, "corpus": cid, "input": text,
                "status": "complete" if resp.complete else "incomplete",
                "outputs": [
                    {"output": render_output(h.output, model.token_level),
                     "cost": round(h.cost, 6)}
                    for h in resp.hypotheses
                ],
                "steps": resp.trace,
            }
            report["normal"].append(payload)
        except Exception:  # noqa: BLE001  记录为失败而非崩溃
            failures += 1
            report["normal"].append({
                "run_id": run_id, "corpus": cid, "input": text,
                "status": "error", "traceback": traceback.format_exc(),
            })

    # ---- 组合 vs 顺序执行交叉核验 ----
    for cid, text in NORMAL_CASES:
        model = ingested[cid][1]
        try:
            a = run_query(model, text, k=8, budget=200_000)
            b = transduce_stagewise(model, text, k=8, budget=200_000)
            sig_a = [(render_output(h.output, model.token_level), round(h.cost, 6))
                     for h in a.hypotheses]
            sig_b = [(render_output(h.output, model.token_level), round(h.cost, 6))
                     for h in b.hypotheses]
            agree = sig_a == sig_b
            failures += 0 if agree else 1
            report["cross_checks"].append({
                "corpus": cid, "input": text, "agree": agree,
                "composed": sig_a, "stagewise": sig_b,
            })
        except Exception:  # noqa: BLE001
            failures += 1
            report["cross_checks"].append({
                "corpus": cid, "input": text, "agree": False,
                "traceback": traceback.format_exc(),
            })

    # ---- 服务层异常类别 ----
    for cid, text, expected in ABNORMAL_CASES:
        model = ingested[cid][1]
        run_id = new_run_id()
        try:
            run_query(model, text, k=8)
            verdict = "UNEXPECTED_SUCCESS"
            failures += 1
            category = None
        except QueryError as exc:
            category = exc.category.value
            ok = exc.category is expected
            verdict = "correctly_rejected" if ok else "WRONG_CATEGORY"
            failures += 0 if ok else 1
        logger.event(run_id, "demo_abnormal", corpus=cid, input=text,
                     category=category, expected=expected.value, verdict=verdict)
        report["abnormal"].append({
            "run_id": run_id, "corpus": cid, "input": text,
            "category": category, "expected": expected.value, "verdict": verdict,
        })

    # ---- 预算耗尽：未完成而非失败 ----
    eps_model = ingested["epsilon_ambiguity_demo"][1]
    resp = run_query(eps_model, "ab", k=100, budget=12)
    budget_case = {"input": "ab", "budget": 12, "complete": resp.complete,
                   "found": len(resp.hypotheses), "pops": resp.pops,
                   "verdict": "incomplete" if not resp.complete else "WRONG"}
    if resp.complete:
        failures += 1
    report["abnormal"].append(budget_case)

    # ---- 核心层边界：负代价环必须报错 ----
    neg = edit_cascade_fst(
        substitutions={("a", "X"): 0.0}, insertions={}, deletions={},
        alphabet=["a"], name="neg",
    )
    neg.add_arc(0, 0, EPSILON, EPSILON, -0.1)  # 负代价 ε:ε 自环
    chained = compose(chain_acceptor(["a"]), neg)
    try:
        nbest_paths(chained, k=3)
        neg_verdict = "UNEXPECTED_SUCCESS"
        failures += 1
    except NegativeCycleError as exc:
        neg_verdict = "correctly_raised_negative_cycle"
        neg_detail = list(exc.cycle_states)
    report["core_edge_cases"].append(
        {"case": "negative_cycle", "verdict": neg_verdict,
         "cycle_states": neg_detail if neg_verdict.startswith("correctly") else None}
    )

    # ---- 核心层边界：零代价 ε:ε 环不死循环 ----
    zero = edit_cascade_fst({}, {}, {}, alphabet=["a"], name="zero")
    zero.add_arc(0, 0, EPSILON, EPSILON, 0.0)
    zr = nbest_paths(compose(chain_acceptor(["a"]), zero), k=3, budget=2000)
    ok_zero = [h.output for h in zr.hypotheses] == ["a"] and zr.complete
    failures += 0 if ok_zero else 1
    report["core_edge_cases"].append(
        {"case": "zero_cost_epsilon_loop", "verdict": "terminated" if ok_zero else "WRONG",
         "outputs": [h.output for h in zr.hypotheses]}
    )

    report["failures"] = failures
    report["verdict"] = "ALL_OK" if failures == 0 else f"{failures}_FAILURES"

    out_json = RESULTS / "demo_results.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    lines = [
        f"run_id={report['run_id']}  service_version={__version__}  "
        f"python={report['python']}",
        f"总体判定：{report['verdict']}",
        "",
        "== 正常查询（代价升序，平手字典序） ==",
    ]
    for c in report["normal"]:
        if c["status"] == "error":
            lines.append(f"[ERROR] {c['corpus']} {c['input']!r}")
            continue
        outs = ", ".join(f"{o['output']}({o['cost']})" for o in c["outputs"])
        lines.append(f"{c['corpus']:26} {c['input']!r:14} -> {outs} [{c['status']}]")
    lines += ["", "== 组合 vs 顺序执行 =="]
    for c in report["cross_checks"]:
        lines.append(f"{c['corpus']:26} {c['input']!r:14} agree={c['agree']}")
    lines += ["", "== 异常/边界用例 =="]
    for c in report["abnormal"]:
        lines.append(json.dumps(c, ensure_ascii=False))
    for c in report["core_edge_cases"]:
        lines.append(json.dumps(c, ensure_ascii=False))
    out_txt = RESULTS / "demo_results.txt"
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    print(f"\n已写出：{out_json}\n        {out_txt}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
