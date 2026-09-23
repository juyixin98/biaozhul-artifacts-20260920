#!/usr/bin/env python3
"""端到端测试：以真实子进程运行 gridtopo_check，断言退出码与报告内容。

不依赖第三方库（仅标准库）。任何断言失败以退出码 1 返回。
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "build" / "gridtopo_check"
SAMPLES = ROOT / "samples"


def run(sample_name):
    proc = subprocess.run(
        [str(BIN), str(SAMPLES / sample_name)],
        capture_output=True,
        text=True,
    )
    return proc.returncode, json.loads(proc.stdout), proc.stderr


def issue_types(report, key):
    return [i["type"] for i in report[key]]


FAILURES = []


def expect(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def case(sample, expected_code, assertions):
    print(f"CASE {sample} (expect exit {expected_code})")
    code, report, stderr = run(sample)
    expect("exit_code", code == expected_code, f"got {code}, stderr={stderr}")
    if report.get("status") != "request_error":
        for label, fn in assertions:
            expect(label, fn(report))
    print()


def main():
    if not BIN.exists():
        print(f"binary not found: {BIN}", file=sys.stderr)
        return 2

    # ---- 1. 封闭四面体：无任何问题 ----
    case("request_tetrahedron_closed.json", 0, [
        ("status=valid", lambda r: r["status"] == "valid"),
        ("no geometric issues", lambda r: len(r["geometric_degeneracies"]) == 0),
        ("no topological issues", lambda r: len(r["topological_errors"]) == 0),
        ("watertight", lambda r: r["summary"]["watertight"] is True),
        ("no boundary edges", lambda r: r["summary"]["boundary_edge_count"] == 0),
        ("1 component / 4 faces / 4 vertices",
         lambda r: (r["summary"]["connected_component_count"] == 1
                    and r["connected_components"][0]["face_count"] == 4
                    and r["connected_components"][0]["vertex_count"] == 4)),
    ])

    # ---- 2. 开洞曲面：只有边界边，没有错误 ----
    case("request_open_hole_surface.json", 0, [
        ("status=valid", lambda r: r["status"] == "valid"),
        ("8 boundary edges (outer 4 + inner 4)",
         lambda r: r["summary"]["boundary_edge_count"] == 8),
        ("not watertight", lambda r: r["summary"]["watertight"] is False),
        ("1 component with 8 faces",
         lambda r: (r["summary"]["connected_component_count"] == 1
                    and r["connected_components"][0]["face_count"] == 8)),
        ("no orientation conflict",
         lambda r: r["summary"]["orientation_conflict_count"] == 0),
    ])

    # ---- 3. 三面共边：非流形边，报告必须含 3 个面 ID ----
    case("request_three_faces_one_edge.json", 1, [
        ("1 nonmanifold edge",
         lambda r: r["summary"]["nonmanifold_edge_count"] == 1),
        ("incident face ids are f0,f1,f2",
         lambda r: [i for i in r["topological_errors"]
                    if i["type"] == "nonmanifold_edge"][0]["face_ids"] == ["f0", "f1", "f2"]),
        ("edge endpoints reported",
         lambda r: [i for i in r["topological_errors"]
                    if i["type"] == "nonmanifold_edge"][0]["vertex_ids"] == ["v0", "v1"]),
        ("metric incident_face_count=3",
         lambda r: [i for i in r["topological_errors"]
                    if i["type"] == "nonmanifold_edge"][0]["metric"]["value"] == 3),
    ])

    # ---- 4. 重复面（反向绕序）----
    case("request_duplicate_face.json", 1, [
        ("duplicate_face reported",
         lambda r: issue_types(r, "topological_errors") == ["duplicate_face"]),
        ("detail opposite_winding",
         lambda r: r["topological_errors"][0]["detail"] == "opposite_winding"),
        ("copies metric = 2",
         lambda r: r["topological_errors"][0]["metric"]["value"] == 2),
        ("edges still manifold (incidence=2)",
         lambda r: r["summary"]["nonmanifold_edge_count"] == 0),
    ])

    # ---- 5. 方向冲突（两面同向遍历共享边，归入非流形边并标注方向冲突）----
    case("request_orientation_conflict.json", 1, [
        ("nonmanifold_edge reported",
         lambda r: "nonmanifold_edge" in issue_types(r, "topological_errors")),
        ("conflict face ids f0,f1",
         lambda r: [i for i in r["topological_errors"]
                    if i["type"] == "nonmanifold_edge"][0]["face_ids"] == ["f0", "f1"]),
        ("orientation_conflict_count == 1",
         lambda r: r["summary"]["orientation_conflict_count"] == 1),
        ("4 boundary edges",
         lambda r: r["summary"]["boundary_edge_count"] == 4),
    ])

    # ---- 6. 孤立点 ----
    case("request_isolated_vertex.json", 1, [
        ("isolated v3",
         lambda r: r["isolated_vertices"] == ["v3"]
                   and r["summary"]["isolated_vertex_count"] == 1),
        ("component counts faces only via valid faces",
         lambda r: r["connected_components"][0]["vertex_count"] == 3),
    ])

    # ---- 7. 几何退化：几何问题与拓扑问题分离 ----
    case("request_geometric_degeneracy.json", 1, [
        ("zero_area_triangle is geometric",
         lambda r: "zero_area_triangle" in issue_types(r, "geometric_degeneracies")),
        ("coincident_vertices is geometric",
         lambda r: "coincident_vertices" in issue_types(r, "geometric_degeneracies")),
        ("zero-area face excluded, no components",
         lambda r: r["summary"]["connected_component_count"] == 0),
        ("area metric printed",
         lambda r: [i for i in r["geometric_degeneracies"]
                    if i["type"] == "zero_area_triangle"][0]["metric"]["name"] == "area"),
        ("isolated vertices kept as topological errors",
         lambda r: r["summary"]["isolated_vertex_count"] == 5),
    ])

    # ---- 8. 坏请求：越界顶点引用 -> exit 2 ----
    print("CASE request_bad_reference.json (expect exit 2)")
    code, report, stderr = run("request_bad_reference.json")
    expect("exit_code", code == 2, f"got {code}")
    expect("request_error/mesh_load",
           report.get("status") == "request_error" and report.get("phase") == "mesh_load")
    print()

    # ---- 9. stdin 管道输入 ----
    print("CASE stdin pipe tetrahedron (expect exit 0)")
    proc = subprocess.run([str(BIN)], input=(SAMPLES / "request_tetrahedron_closed.json").read_text(),
                          capture_output=True, text=True)
    report = json.loads(proc.stdout)
    expect("exit_code", proc.returncode == 0, f"got {proc.returncode}")
    expect("watertight via stdin", report["summary"]["watertight"] is True)
    print()

    # ---- 10. JSON 语法错误 ----
    print("CASE malformed JSON (expect exit 2)")
    proc = subprocess.run([str(BIN)], input="{ not json", capture_output=True, text=True)
    report = json.loads(proc.stdout)
    expect("exit_code", proc.returncode == 2, f"got {proc.returncode}")
    expect("phase=json_parse with line/column",
           report.get("phase") == "json_parse" and "line" in report and "column" in report)
    print()

    if FAILURES:
        print(f"{len(FAILURES)} e2e assertion(s) FAILED: {FAILURES}")
        return 1
    print("All e2e cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
