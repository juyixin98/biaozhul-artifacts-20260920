"""依赖图：环检测（Tarjan SCC、自环）、多版本、路径枚举、根判定。"""
from __future__ import annotations

import pytest

from sbom_risk.graph import GraphError, build_graph
from sbom_risk.models import SBOM


def pkg(ecosystem, name, version, deps=(), is_root=None):
    return {
        "ecosystem": ecosystem,
        "name": name,
        "version": version,
        "dependencies": list(deps),
        **({"is_root": is_root} if is_root is not None else {}),
    }


def iid(eco, name, ver):
    return f"{eco}:{name}@{ver}"


class TestCycles:
    def test_two_node_cycle_detected(self):
        sbom = SBOM.model_validate({
            "packages": [
                pkg("npm", "root", "1.0.0", ["npm:a@1.0.0"], is_root=True),
                pkg("npm", "a", "1.0.0", ["npm:b@2.0.0"]),
                pkg("npm", "b", "2.0.0", ["npm:a@1.0.0"]),
            ]
        })
        g = build_graph(sbom)
        assert g.cycles == [[iid("npm", "a", "1.0.0"), iid("npm", "b", "2.0.0")]]

    def test_three_node_cycle_with_tail(self):
        sbom = SBOM.model_validate({
            "packages": [
                pkg("npm", "r", "1.0.0", ["npm:x@1.0.0"], is_root=True),
                pkg("npm", "x", "1.0.0", ["npm:y@1.0.0"]),
                pkg("npm", "y", "1.0.0", ["npm:z@1.0.0"]),
                pkg("npm", "z", "1.0.0", ["npm:x@1.0.0"]),
            ]
        })
        g = build_graph(sbom)
        assert len(g.cycles) == 1
        assert set(g.cycles[0]) == {iid("npm", "x", "1.0.0"),
                                    iid("npm", "y", "1.0.0"),
                                    iid("npm", "z", "1.0.0")}

    def test_self_loop_detected(self):
        sbom = SBOM.model_validate({
            "packages": [
                pkg("npm", "root", "1.0.0", ["npm:self@1.0.0"], is_root=True),
                pkg("npm", "self", "1.0.0", ["npm:self@1.0.0"]),
            ]
        })
        g = build_graph(sbom)
        assert g.cycles == [[iid("npm", "self", "1.0.0")]]

    def test_acyclic_graph_has_no_cycles(self):
        sbom = SBOM.model_validate({
            "packages": [
                pkg("npm", "a", "1.0.0", ["npm:b@1.0.0", "npm:c@1.0.0"], is_root=True),
                pkg("npm", "b", "1.0.0", ["npm:d@1.0.0"]),
                pkg("npm", "c", "1.0.0", ["npm:d@1.0.0"]),
                pkg("npm", "d", "1.0.0", []),
            ]
        })
        g = build_graph(sbom)
        assert g.cycles == []


class TestPaths:
    def test_all_simple_paths_in_diamond(self):
        sbom = SBOM.model_validate({
            "packages": [
                pkg("npm", "r", "1.0.0", ["npm:a@1.0.0", "npm:b@1.0.0"], is_root=True),
                pkg("npm", "a", "1.0.0", ["npm:d@1.0.0"]),
                pkg("npm", "b", "1.0.0", ["npm:d@1.0.0"]),
                pkg("npm", "d", "1.0.0", []),
            ]
        })
        g = build_graph(sbom)
        paths = g.find_paths_to(iid("npm", "d", "1.0.0"))
        assert sorted(paths) == sorted([
            ["npm:r@1.0.0", "npm:a@1.0.0", "npm:d@1.0.0"],
            ["npm:r@1.0.0", "npm:b@1.0.0", "npm:d@1.0.0"],
        ])

    def test_paths_through_cycle_are_simple(self):
        # root -> a -> b -> a 成环；查找 b 只能走简单路径，不能无限绕环
        sbom = SBOM.model_validate({
            "packages": [
                pkg("npm", "r", "1.0.0", ["npm:a@1.0.0"], is_root=True),
                pkg("npm", "a", "1.0.0", ["npm:b@1.0.0"]),
                pkg("npm", "b", "1.0.0", ["npm:a@1.0.0"]),
            ]
        })
        g = build_graph(sbom)
        paths = g.find_paths_to(iid("npm", "b", "1.0.0"))
        assert paths == [["npm:r@1.0.0", "npm:a@1.0.0", "npm:b@1.0.0"]]

    def test_unreachable_target_returns_empty(self):
        sbom = SBOM.model_validate({
            "packages": [
                pkg("npm", "r", "1.0.0", ["npm:a@1.0.0"], is_root=True),
                pkg("npm", "a", "1.0.0", []),
                pkg("npm", "orphan", "1.0.0", []),
            ]
        })
        g = build_graph(sbom)
        assert g.find_paths_to(iid("npm", "orphan", "1.0.0")) == []

    def test_multiple_roots_produce_paths(self):
        sbom = SBOM.model_validate({
            "packages": [
                pkg("npm", "r1", "1.0.0", ["npm:d@1.0.0"], is_root=True),
                pkg("npm", "r2", "1.0.0", ["npm:d@1.0.0"], is_root=True),
                pkg("npm", "d", "1.0.0", []),
            ]
        })
        g = build_graph(sbom)
        paths = g.find_paths_to(iid("npm", "d", "1.0.0"))
        assert len(paths) == 2


class TestIdentityAndValidation:
    def test_same_name_different_ecosystems_are_distinct(self):
        sbom = SBOM.model_validate({
            "packages": [
                pkg("npm", "json", "1.0.0", []),
                pkg("pypi", "json", "1.0.0", []),
            ]
        })
        g = build_graph(sbom)
        assert set(g.packages) == {"npm:json@1.0.0", "pypi:json@1.0.0"}

    def test_same_name_version_coexist(self):
        sbom = SBOM.model_validate({
            "packages": [
                pkg("npm", "lib", "1.0.0", []),
                pkg("npm", "lib", "2.0.0", []),
            ]
        })
        g = build_graph(sbom)
        assert set(g.packages) == {"npm:lib@1.0.0", "npm:lib@2.0.0"}

    def test_duplicate_node_rejected(self):
        with pytest.raises(GraphError, match="重复"):
            build_graph(SBOM.model_validate({"packages": [
                pkg("npm", "lib", "1.0.0", []),
                pkg("npm", "lib", "1.0.0", []),
            ]}))

    def test_dangling_dependency_rejected(self):
        with pytest.raises(GraphError, match="不存在"):
            build_graph(SBOM.model_validate({"packages": [
                pkg("npm", "lib", "1.0.0", ["npm:ghost@9.9.9"]),
            ]}))

    def test_implicit_roots_are_indegree_zero(self):
        sbom = SBOM.model_validate({
            "packages": [
                pkg("npm", "a", "1.0.0", ["npm:b@1.0.0"]),
                pkg("npm", "b", "1.0.0", []),
            ]
        })
        g = build_graph(sbom)
        assert g.roots == ["npm:a@1.0.0"]

    def test_explicit_roots_override_indegree(self):
        sbom = SBOM.model_validate({
            "packages": [
                pkg("npm", "a", "1.0.0", ["npm:b@1.0.0"]),
                pkg("npm", "b", "1.0.0", [], is_root=True),
            ]
        })
        g = build_graph(sbom)
        assert g.roots == ["npm:b@1.0.0"]
