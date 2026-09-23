#!/usr/bin/env python3
"""Automated tests for the meshcheck binary.

Each case builds a JSON request, runs build/meshcheck, parses the JSON report,
and asserts on the reported face ids / component statistics. Exit status:
0 = all passed, 1 = failures.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "build", "meshcheck")
OUT = os.path.join(ROOT, "tests", "out")

PASSED = 0
FAILED = 0
FAILURES = []


def run_case(name, request, extra_args=None):
    """Run meshcheck on `request`; returns (rc, report)."""
    os.makedirs(OUT, exist_ok=True)
    req_path = os.path.join(OUT, name + ".request.json")
    with open(req_path, "w") as f:
        json.dump(request, f)
    args = [BIN]
    if extra_args:
        args += extra_args
    args.append(req_path)
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    report = json.loads(proc.stdout) if proc.stdout.strip() else None
    # Persist the report for inspection / the README run log.
    if report is not None:
        with open(os.path.join(OUT, name + ".response.json"), "w") as f:
            json.dump(report, f, indent=2)
    return proc.returncode, report


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        FAILURES.append((name, detail))
        print(f"  FAIL  {name}  {detail}")


def ids(items, key):
    return sorted(x[key] for x in items)


# --------------------------------------------------------------------------
# Case 1: closed, consistently oriented tetrahedron
# --------------------------------------------------------------------------
def test_closed_tetrahedron():
    print("case: closed_tetrahedron")
    req = {
        "name": "closed_tetrahedron",
        "points": [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.5, 0.8660254037844386, 0.0],
            [0.5, 0.2886751345948129, 0.816496580927726],
        ],
        "faces": [
            {"id": 10, "verts": [0, 1, 2]},
            {"id": 11, "verts": [0, 3, 1]},
            {"id": 12, "verts": [0, 2, 3]},
            {"id": 13, "verts": [1, 3, 2]},
        ],
    }
    rc, rep = run_case("01_closed_tetrahedron", req)
    s = rep["summary"]
    check("01 exit code 0", rc == 0, f"rc={rc}")
    check("01 ok=true", rep["ok"] is True)
    check("01 manifold", s["manifold"] is True)
    check("01 orientable", s["orientable"] is True)
    check("01 closed", s["closed"] is True)
    check("01 1 component", s["component_count"] == 1)
    check("01 no non-manifold", s["non_manifold_edge_count"] == 0)
    check("01 no orientation conflict", s["orientation_conflict_count"] == 0)
    check("01 no duplicate faces", s["duplicate_face_count"] == 0)
    check("01 no boundary edges", s["boundary_edge_count"] == 0)
    c = rep["components"][0]
    check("01 component f=4 v=4 b=0 closed",
          (c["face_count"], c["vertex_count"], c["boundary_edges"], c["closed"])
          == (4, 4, 0, True), str(c))
    # Euler characteristic: V - E + F = 4 - 6 + 4 = 2 (sphere)
    check("01 euler characteristic 2", c["vertex_count"] - 6 + c["face_count"] == 2)


# --------------------------------------------------------------------------
# Case 2: open fan surface with a missing wedge + isolated vertex
# --------------------------------------------------------------------------
def test_open_fan():
    print("case: open_fan_surface")
    ring = [
        [1.0, 0.0, 0.0],
        [0.7071067811865476, 0.7071067811865475, 0.0],
        [0.0, 1.0, 0.0],
        [-0.7071067811865475, 0.7071067811865476, 0.0],
        [-1.0, 0.0, 0.0],
        [-0.7071067811865477, -0.7071067811865475, 0.0],
        [0.0, -1.0, 0.0],
        [0.7071067811865475, -0.7071067811865477, 0.0],
    ]
    req = {
        "name": "open_fan_surface",
        "points": [[0.0, 0.0, 0.0]] + ring + [[5.0, 5.0, 5.0]],
        "faces": [[0, i + 1, (i + 1) % 8 + 1] for i in range(7)],
    }
    rc, rep = run_case("02_open_fan_surface", req)
    s = rep["summary"]
    # Open surface -> topology issue (boundary) but not an error category;
    # binary returns 1 only for error categories. Boundary is informational.
    check("02 manifold with boundary", s["manifold"] is True)
    check("02 orientable", s["orientable"] is True)
    check("02 not closed", s["closed"] is False)
    check("02 1 component", s["component_count"] == 1)
    # 7 outer rim edges + 2 edges of the missing wedge = 9 boundary edges
    check("02 9 boundary edges", s["boundary_edge_count"] == 9,
          f"got {s['boundary_edge_count']}")
    check("02 isolated vertex 9", rep["errors"]["isolated_vertices"] == [9],
          str(rep["errors"]["isolated_vertices"]))
    c = rep["components"][0]
    check("02 component f=7 v=9 b=9",
          (c["face_count"], c["vertex_count"], c["boundary_edges"]) == (7, 9, 9), str(c))
    check("02 boundary_edges entries carry component 0",
          all(e["component"] == 0 for e in rep["boundary_edges"]))


# --------------------------------------------------------------------------
# Case 3: three triangles sharing one edge + disconnected second component
# --------------------------------------------------------------------------
def test_three_fans():
    print("case: three_fans_non_manifold")
    req = {
        "name": "three_fans_non_manifold",
        "points": [
            [0.0, 0.0, 0.0], [1.0, 0.0, 0.0],
            [0.5, 0.8, 0.2], [0.5, -0.3, 0.9], [0.2, 0.4, -0.8],
            [3.0, 3.0, 0.0], [4.0, 3.0, 0.0], [3.5, 3.8, 0.0],
            [9.0, 9.0, 9.0],
        ],
        "faces": [
            {"id": 1, "verts": [0, 1, 2]},
            {"id": 2, "verts": [0, 1, 3]},
            {"id": 3, "verts": [0, 1, 4]},
            {"id": 4, "verts": [5, 6, 7]},
        ],
    }
    rc, rep = run_case("03_three_fans_non_manifold", req)
    s = rep["summary"]
    check("03 exit code 1", rc == 1, f"rc={rc}")
    check("03 ok=false", rep["ok"] is False)
    check("03 1 non-manifold edge", s["non_manifold_edge_count"] == 1)
    nm = rep["errors"]["non_manifold_edges"]
    check("03 edge [0,1]", nm[0]["edge"] == [0, 1], str(nm))
    check("03 face_ids [1,2,3]", nm[0]["face_ids"] == [1, 2, 3], str(nm))
    check("03 2 components", s["component_count"] == 2, str(s))
    comps = {c["component"]: c for c in rep["components"]}
    check("03 comp0 f=3 v=5 nm=1 open",
          (comps[0]["face_count"], comps[0]["vertex_count"],
           comps[0]["non_manifold_edges"], comps[0]["closed"])
          == (3, 5, 1, False), str(comps[0]))
    check("03 comp1 f=1 v=3 b=3 open",
          (comps[1]["face_count"], comps[1]["vertex_count"],
           comps[1]["boundary_edges"], comps[1]["closed"])
          == (1, 3, 3, False), str(comps[1]))
    check("03 non-manifold edge assigned comp0", nm[0]["component"] == 0)
    check("03 isolated vertex 8", rep["errors"]["isolated_vertices"] == [8])
    check("03 not manifold", s["manifold"] is False)


# --------------------------------------------------------------------------
# Case 4: duplicate faces (same winding + reversed winding)
# --------------------------------------------------------------------------
def test_duplicate_faces():
    print("case: duplicate_faces")
    req = {
        "points": [[0, 0, 0], [1, 0, 0], [0, 1, 0]],
        "faces": [
            {"id": 100, "verts": [0, 1, 2]},
            {"id": 101, "verts": [0, 1, 2]},  # exact duplicate
            {"id": 102, "verts": [2, 0, 1]},  # same triangle, cyclic rotation
        ],
    }
    rc, rep = run_case("04_duplicate_faces", req)
    s = rep["summary"]
    check("04 exit code 1", rc == 1, f"rc={rc}")
    check("04 2 duplicate face ids",
          rep["errors"]["duplicate_faces"] == [101, 102],
          str(rep["errors"]["duplicate_faces"]))
    group = rep["duplicate_face_groups"][0]
    check("04 group contains [100,101,102]", group == [100, 101, 102], str(group))
    # First copy is kept for analysis -> single triangle remains, 3 boundary edges
    check("04 faces_analyzed=1", s["faces_analyzed"] == 1)
    check("04 boundary edges 3", s["boundary_edge_count"] == 3)
    check("04 1 component", s["component_count"] == 1)


# --------------------------------------------------------------------------
# Case 5: orientation conflict — closed tetra with one flipped face
# --------------------------------------------------------------------------
def test_orientation_conflict():
    print("case: orientation_conflict")
    req = {
        "points": [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.5, 0.8660254037844386, 0.0],
            [0.5, 0.2886751345948129, 0.816496580927726],
        ],
        # face 13 reversed -> each of its 3 edges winds the same way as its neighbour
        "faces": [[0, 1, 2], [0, 3, 1], [0, 2, 3], [1, 2, 3]],
    }
    rc, rep = run_case("05_orientation_conflict", req)
    s = rep["summary"]
    check("05 exit code 1", rc == 1, f"rc={rc}")
    check("05 3 orientation conflicts", s["orientation_conflict_count"] == 3,
          str(s))
    oc = rep["errors"]["orientation_conflicts"]
    reported_faces = sorted(f for e in oc for f in e["face_ids"])
    # Conflicts must implicate the flipped face (input index 3) on every edge.
    check("05 every conflict involves face 3",
          all(3 in e["face_ids"] for e in oc), str(oc))
    check("05 implicates faces {0,1,2,3}",
          sorted(set(reported_faces)) == [0, 1, 2, 3], str(reported_faces))
    check("05 not orientable", s["orientable"] is False)
    check("05 incidence-closed (watertight)", s["closed"] is True,
          "closed is incidence-based even with inconsistent winding")
    check("05 zero boundary edges", s["boundary_edge_count"] == 0)


# --------------------------------------------------------------------------
# Case 6: geometric degeneracy kept separate from topology
# --------------------------------------------------------------------------
def test_degenerate():
    print("case: geometric_degeneracy")
    req = {
        "points": [[0, 0, 0], [1, 0, 0], [2, 0, 0], [0, 1, 0]],
        "faces": [
            [0, 1, 2],  # collinear -> zero area (degenerate)
            [0, 1, 3],  # valid triangle
        ],
    }
    rc, rep = run_case("06_degenerate", req)
    check("06 degenerate face id [0]",
          rep["geometric_degeneracies"]["degenerate_faces"] == [0],
          str(rep["geometric_degeneracies"]))
    check("06 no topology error field populated",
          rep["errors"]["non_manifold_edges"] == []
          and rep["errors"]["orientation_conflicts"] == []
          and rep["errors"]["duplicate_faces"] == [])
    s = rep["summary"]
    check("06 faces_analyzed=1 (degenerate excluded)", s["faces_analyzed"] == 1)
    # Degenerate face is not a topology error -> exit code 0
    check("06 exit code 0 (degeneracy is not a topology error)", rc == 0, f"rc={rc}")
    check("06 vertex 2 is isolated after exclusion",
          rep["errors"]["isolated_vertices"] == [2])


# --------------------------------------------------------------------------
# Case 7: coincident vertices get merged (near-duplicate coordinates)
# --------------------------------------------------------------------------
def test_duplicate_vertices():
    print("case: duplicate_vertices_merge")
    req = {
        # vertex 3 coincides with 1 within tolerance
        "points": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [1.0 + 1e-12, 0, 0]],
        "faces": [[0, 1, 2], [0, 3, 2]],
    }
    rc, rep = run_case("07_duplicate_vertices", req)
    dv = rep["geometric_degeneracies"]["duplicate_vertices"]
    check("07 reports merge pair [1,3]", dv == [[1, 3]], str(dv))
    s = rep["summary"]
    # After merge the two faces share [0,2] and each contains the merged
    # vertex -> they form a valid 2-face patch (edge 1-2 twice? check below)
    # Face A edges: 0-1,1-2,0-2 ; face B edges: 0-1,1-2,0-2 -> duplicates!
    check("07 duplicate faces after merge", s["duplicate_face_count"] == 1, str(s))
    check("07 merge feeds topology analysis", s["faces_analyzed"] == 1)


# --------------------------------------------------------------------------
# Case 8: malformed requests -> request_errors, deterministic failure
# --------------------------------------------------------------------------
def test_request_errors():
    print("case: request_errors")
    rc, rep = run_case("08a_bad_index", {
        "points": [[0, 0, 0], [1, 0, 0], [0, 1, 0]],
        "faces": [[0, 1, 9]],
    })
    check("08a exit 1", rc == 1, f"rc={rc}")
    check("08a out-of-range error",
          any("out of range" in e for e in rep["errors"]["request_errors"]),
          str(rep["errors"]["request_errors"]))

    rc, rep = run_case("08b_wrong_arity", {
        "points": [[0, 0, 0], [1, 0, 0], [0, 1, 0]],
        "faces": [[0, 1]],
    })
    check("08b bad face shape error",
          any("expected three vertex indices" in e
              for e in rep["errors"]["request_errors"]),
          str(rep["errors"]["request_errors"]))

    rc, rep = run_case("08c_nan_point", {
        "points": [[0, 0, 0], [1, 0, 0], [0, 1, float("nan")]],
        "faces": [[0, 1, 2]],
    })
    check("08c invalid point + dangling face",
          any("expected three finite numbers" in e
              for e in rep["errors"]["request_errors"]),
          str(rep["errors"]["request_errors"]))
    check("08c face rejected", rep["summary"]["faces_accepted"] == 0)


# --------------------------------------------------------------------------
# Case 9: stdin input and JSON parse failure exit code 2
# --------------------------------------------------------------------------
def test_stdin_and_bad_json():
    print("case: stdin_and_bad_json")
    req = {"points": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "faces": [[0, 1, 2]]}
    proc = subprocess.run([BIN], input=json.dumps(req), capture_output=True, text=True)
    check("09 stdin works rc=0", proc.returncode == 0, f"rc={proc.returncode}")
    rep = json.loads(proc.stdout)
    check("09 stdin report closed=false", rep["summary"]["closed"] is False)

    proc = subprocess.run([BIN], input="{not json", capture_output=True, text=True)
    check("09 malformed JSON rc=2", proc.returncode == 2, f"rc={proc.returncode}")
    check("09 stderr mentions offset", "offset" in proc.stderr, proc.stderr)


# --------------------------------------------------------------------------
# Case 9b: --eps-rel 0 disables coincident-vertex merging
# --------------------------------------------------------------------------
def test_merge_disabled():
    print("case: merge_disabled_eps_rel_0")
    req = {
        "points": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [1.000000000001, 0, 0]],
        "faces": [[0, 1, 2], [0, 3, 2]],
    }
    rc, rep = run_case("09b_merge_disabled", req, extra_args=["--eps-rel", "0"])
    dv = rep["geometric_degeneracies"]["duplicate_vertices"]
    check("09b no vertex merge", dv == [], str(dv))
    check("09b no duplicate faces", rep["summary"]["duplicate_face_count"] == 0)
    # Two distinct triangles sharing only edge [0,2]: 4 boundary edges.
    check("09b 4 boundary edges (shared [0,2])",
          rep["summary"]["boundary_edge_count"] == 4,
          str(rep["summary"]["boundary_edge_count"]))
    check("09b 1 component (shared edge connects them)",
          rep["summary"]["component_count"] == 1)


# --------------------------------------------------------------------------
# Case 10: two disjoint closed tetrahedra -> 2 closed components
# --------------------------------------------------------------------------
def test_two_components():
    print("case: two_disjoint_tetrahedra")
    tet_pts = [
        [0, 0, 0], [1, 0, 0],
        [0.5, 0.8660254037844386, 0],
        [0.5, 0.2886751345948129, 0.816496580927726],
    ]
    pts = tet_pts + [[p[0] + 5, p[1] + 5, p[2] + 5] for p in tet_pts]
    base = [[0, 1, 2], [0, 3, 1], [0, 2, 3], [1, 3, 2]]
    faces = base + [[a + 4, b + 4, c + 4] for a, b, c in base]
    req = {"points": pts, "faces": faces}
    rc, rep = run_case("10_two_tets", req)
    s = rep["summary"]
    check("10 rc=0", rc == 0, f"rc={rc}")
    check("10 2 closed components", s["component_count"] == 2 and s["closed"] is True)
    check("10 each component f=4 v=4",
          all((c["face_count"], c["vertex_count"], c["closed"]) == (4, 4, True)
              for c in rep["components"]),
          str(rep["components"]))
    check("10 8 vertices total", s["vertices_input"] == 8)


def main():
    if not os.path.exists(BIN):
        print(f"binary not found: {BIN}; run `make` first", file=sys.stderr)
        return 2
    for t in [
        test_closed_tetrahedron, test_open_fan, test_three_fans,
        test_duplicate_faces, test_orientation_conflict, test_degenerate,
        test_duplicate_vertices, test_request_errors,
        test_stdin_and_bad_json, test_merge_disabled, test_two_components,
    ]:
        t()
    print(f"\n{PASSED} passed, {FAILED} failed")
    if FAILED:
        for name, detail in FAILURES:
            print(f"  FAILED: {name} {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
