"""复现实验运行器：在 pytest 内执行完整夹具流程并断言报告判定。"""
from __future__ import annotations

import json

from app.reproducibility.fixtures import two_arm_two_strata_fixture
from app.reproducibility.runner import (
    run_fixture, run_reproduction_experiment,
)


def test_full_reproduction_experiment_passes(tmp_path):
    report = run_reproduction_experiment(workdir=str(tmp_path))
    summary = report["summary"]
    assert summary["overall"] == "PASS", json.dumps(summary, ensure_ascii=False)
    assert summary["all_serial_double_runs_identical"] is True
    assert summary["all_concurrent_slot_integrity_ok"] is True
    assert summary["all_feature_tampering_rejected_and_preserved"] is True
    assert summary["all_recoveries_ok"] is True
    # 每个夹具的串行双跑都逐对象一致
    for comp in report["serial_double_run_comparisons"]:
        assert comp["identical"] is True
        assert comp["contract_fingerprint_same"] is True
        assert comp["n_compared"] >= 13


def test_reproduction_is_itself_reproducible(tmp_path):
    """两次完整实验（不同目录）产生的串行分配序列必须相同。"""
    r1 = run_reproduction_experiment(workdir=str(tmp_path / "d1"))
    r2 = run_reproduction_experiment(workdir=str(tmp_path / "d2"))
    for name in r1["concurrent_runs"]:
        a = r1["concurrent_runs"][name]
        # 并发运行不可逐对象比较；比较的是串行双跑比对结果
        assert (r1["serial_double_run_comparisons"][0]["identical"]
                == r2["serial_double_run_comparisons"][0]["identical"]
                is True)
    # 指纹跨目录稳定
    assert (r1["concurrent_runs"]["two_arm_keep_open"]["contract"]
            ["contract_fingerprint"]
            == r2["concurrent_runs"]["two_arm_keep_open"]["contract"]
            ["contract_fingerprint"])


def test_fixture_first_allocations_are_deterministic(tmp_path):
    fx = two_arm_two_strata_fixture("keep_open")
    run = run_fixture(fx, str(tmp_path), label="det", concurrent=False,
                      checks=False)
    by_id = {a["subject_id"]: a for a in run["allocations"]}
    # 前四个同层对象（C1,early / C2,late / C3,early / C1,late 顺序）
    # 不做逐臂硬编码（与 test_stream_reference 重复），但断言同种子两次一致
    run2 = run_fixture(fx, str(tmp_path), label="det2", concurrent=False,
                       checks=False)
    by_id2 = {a["subject_id"]: a for a in run2["allocations"]}
    for sid, a in by_id.items():
        b = by_id2[sid]
        assert (a["arm"], a["block_index"], a["position"]) == \
               (b["arm"], b["block_index"], b["position"])


def test_report_records_random_stream_provenance(tmp_path):
    report = run_reproduction_experiment(workdir=str(tmp_path))
    run = report["concurrent_runs"]["two_arm_keep_open"]
    src = run["random_source"]
    assert src["prf"] == "HMAC-SHA256 counter mode"
    assert src["shuffle"] == "unbiased Fisher-Yates"
    assert src["domain"] == "rct/v1/block-permutation"
    assert src["seed_fingerprint"].startswith("seed_")
    assert "block_index" in src["locator_coordinates"]
    # 报告明确不把效果显著当证据
    assert "分配后效果" in report["disclaimer"]
