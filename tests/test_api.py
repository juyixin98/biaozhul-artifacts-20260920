"""JSON 接口层测试：校验、失败状态、截断报告、等权路径、预算。"""

from __future__ import annotations

import unittest

from mospp.api import run_request
from mospp.errors import (
    DEFAULT_NODE_LABEL_CAP,
    DEFAULT_TOTAL_LABEL_CAP,
    SIMPLE_MODE_MAX_NODES,
    RequestError,
)
from mospp.api import parse_request


def base_request():
    return {
        "graph": {
            "nodes": ["A", "B", "C", "D"],
            "edges": [
                {"source": "A", "target": "B", "time": 1, "cost": 4},
                {"source": "A", "target": "C", "time": 2, "cost": 2},
                {"source": "B", "target": "D", "time": 3, "cost": 1},
                {"source": "C", "target": "D", "time": 4, "cost": 1},
                {"source": "B", "target": "C", "time": 1, "cost": 1},
            ],
        },
        "source": "A",
        "target": "D",
    }


class TestSuccessResponses(unittest.TestCase):
    def test_basic_ok(self):
        r = run_request(base_request())
        self.assertEqual(r["status"], "ok")
        self.assertFalse(r["truncated"])
        pts = [(p["time"], p["cost"]) for p in r["pareto_front"]]
        self.assertEqual(pts, [(4, 5), (6, 3)])
        self.assertEqual(r["statistics"]["pareto_front_size"], 2)
        self.assertTrue(r["statistics"]["paths_verification_ok"])

    def test_path_reconstruction_payload(self):
        r = run_request(base_request())
        first = r["pareto_front"][0]
        self.assertEqual(first["nodes"], ["A", "B", "D"])
        # 未提供 edge id：返回 {source,target,index}
        self.assertEqual(
            first["edges"],
            [
                {"source": "A", "target": "B", "index": 0},
                {"source": "B", "target": "D", "index": 0},
            ],
        )

    def test_edge_ids_in_response(self):
        req = base_request()
        for i, e in enumerate(req["graph"]["edges"]):
            e["id"] = f"edge-{i}"
        r = run_request(req)
        self.assertEqual(
            r["pareto_front"][0]["edges"], ["edge-0", "edge-2"]
        )

    def test_integer_node_ids_preserved(self):
        req = base_request()
        req["graph"]["nodes"] = [10, 20, 30, 40]
        mapping = {"A": 10, "B": 20, "C": 30, "D": 40}
        for e in req["graph"]["edges"]:
            e["source"] = mapping[e["source"]]
            e["target"] = mapping[e["target"]]
        req["source"], req["target"] = 10, 40
        r = run_request(req)
        self.assertEqual(r["pareto_front"][0]["nodes"], [10, 20, 40])

    def test_no_path_status(self):
        req = base_request()
        req["graph"]["edges"] = [
            {"source": "A", "target": "B", "time": 1, "cost": 1}
        ]
        req["target"] = "C"  # C 不可达
        r = run_request(req)
        self.assertEqual(r["status"], "no_path")
        self.assertEqual(r["pareto_front"], [])

    def test_source_equals_target(self):
        r = run_request({**base_request(), "target": "A"})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(
            [(p["time"], p["cost"]) for p in r["pareto_front"]], [(0, 0)]
        )
        self.assertEqual(r["pareto_front"][0]["nodes"], ["A"])
        self.assertEqual(r["pareto_front"][0]["edges"], [])


class TestValidationErrors(unittest.TestCase):
    def _expect(self, req, code):
        with self.assertRaises(RequestError) as ctx:
            parse_request(req)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_request_not_object(self):
        self._expect([1, 2], "invalid_request")

    def test_invalid_json_handled_at_cli_layer(self):
        # parse_request 只接受已解析对象
        self._expect("string", "invalid_request")

    def test_unknown_top_field(self):
        exc = self._expect({**base_request(), "hack": 1}, "unknown_field")
        self.assertEqual(exc.field, "hack")

    def test_unknown_edge_field(self):
        req = base_request()
        req["graph"]["edges"][0]["wheight"] = 3
        self._expect(req, "unknown_field")

    def test_missing_required_edge_fields(self):
        req = base_request()
        del req["graph"]["edges"][0]["cost"]
        exc = self._expect(req, "missing_field")
        self.assertEqual(exc.field, "graph.edges[0].cost")

    def test_negative_weight_rejected(self):
        req = base_request()
        req["graph"]["edges"][0]["time"] = -0.1
        exc = self._expect(req, "negative_weight")
        self.assertEqual(exc.field, "graph.edges[0].time")

    def test_nan_inf_weight_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            req = base_request()
            req["graph"]["edges"][0]["time"] = bad
            with self.assertRaises(RequestError) as ctx:
                parse_request(req)
            # NaN 与 ±Inf 都不是合法的有限权重，一律 invalid_weight
            self.assertEqual(ctx.exception.code, "invalid_weight")

    def test_weight_as_string_rejected(self):
        req = base_request()
        req["graph"]["edges"][0]["time"] = "1"
        self._expect(req, "invalid_weight")

    def test_duplicate_node(self):
        req = base_request()
        req["graph"]["nodes"][1] = "A"
        self._expect(req, "duplicate_node")

    def test_unknown_node_reference(self):
        req = base_request()
        req["graph"]["edges"][0]["target"] = "Z"
        self._expect(req, "unknown_node")

    def test_unknown_source_target(self):
        self._expect({**base_request(), "source": "Z"}, "unknown_node")
        self._expect({**base_request(), "target": "Z"}, "unknown_node")

    def test_invalid_id(self):
        req = base_request()
        req["graph"]["nodes"] = ["", "B", "C", "D"]
        self._expect(req, "invalid_id")

    def test_duplicate_edge_id(self):
        req = base_request()
        req["graph"]["edges"][0]["id"] = "x"
        req["graph"]["edges"][1]["id"] = "x"
        self._expect(req, "duplicate_edge_id")

    def test_empty_nodes_or_edges_type(self):
        req = base_request()
        req["graph"]["nodes"] = []
        self._expect(req, "invalid_graph")
        req2 = base_request()
        req2["graph"]["edges"] = {}
        self._expect(req2, "invalid_graph")

    def test_bad_mode(self):
        self._expect({**base_request(), "mode": "fast"}, "invalid_mode")

    def test_bad_tolerance(self):
        self._expect({**base_request(), "atol": -1e-9}, "invalid_tolerance")
        self._expect({**base_request(), "rtol": 1.0}, "invalid_tolerance")
        self._expect({**base_request(), "atol": "x"}, "invalid_tolerance")

    def test_bad_cap(self):
        self._expect(
            {**base_request(), "node_label_cap": 0}, "invalid_cap"
        )
        self._expect(
            {**base_request(), "total_label_cap": -3}, "invalid_cap"
        )

    def test_bad_budget(self):
        self._expect(
            {**base_request(), "time_budget": -1}, "invalid_budget"
        )
        self._expect(
            {**base_request(), "cost_budget": "x"}, "invalid_budget"
        )

    def test_simple_mode_size_limit(self):
        n = SIMPLE_MODE_MAX_NODES + 1
        req = {
            "graph": {
                "nodes": [f"v{i}" for i in range(n)],
                "edges": [
                    {"source": "v0", "target": "v1", "time": 1, "cost": 1}
                ],
            },
            "source": "v0",
            "target": "v1",
        }
        with self.assertRaises(Exception) as ctx:
            parse_request(req)
        self.assertEqual(ctx.exception.code, "limit_exceeded")
        # walk 模式允许
        req["mode"] = "walk"
        params = parse_request(req)
        self.assertEqual(params["mode"], "walk")

    def test_default_caps(self):
        params = parse_request(base_request())
        self.assertEqual(params["node_label_cap"], DEFAULT_NODE_LABEL_CAP)
        self.assertEqual(params["total_label_cap"], DEFAULT_TOTAL_LABEL_CAP)


class TestBudgetPruningApi(unittest.TestCase):
    def test_budget_filters_front(self):
        # 无预算前沿 [(4,5),(6,3)]；时间预算 5 应保留两条；
        # 费用预算 4 只保留 (6,3)
        r_all = run_request(base_request())
        self.assertEqual(
            [(p["time"], p["cost"]) for p in r_all["pareto_front"]],
            [(4, 5), (6, 3)],
        )
        r = run_request({**base_request(), "cost_budget": 4})
        self.assertEqual(
            [(p["time"], p["cost"]) for p in r["pareto_front"]], [(6, 3)]
        )
        self.assertGreaterEqual(r["statistics"]["budget_pruned"], 1)

    def test_tight_budget_no_path(self):
        r = run_request({**base_request(), "time_budget": 1})
        self.assertEqual(r["status"], "no_path")
        self.assertEqual(r["pareto_front"], [])

    def test_budget_boundary_is_inclusive(self):
        # time_budget=4 必须保留 (4,5)
        r = run_request({**base_request(), "time_budget": 4})
        pts = [(p["time"], p["cost"]) for p in r["pareto_front"]]
        self.assertIn((4, 5), pts)


class TestEqualLabelsApi(unittest.TestCase):
    def _req(self):
        return {
            "graph": {
                "nodes": [0, 1, 2, 3],
                "edges": [
                    {"source": 0, "target": 1, "time": 1, "cost": 2},
                    {"source": 0, "target": 2, "time": 1, "cost": 3},
                    {"source": 1, "target": 3, "time": 1, "cost": 2},
                    {"source": 2, "target": 3, "time": 1, "cost": 1},
                ],
            },
            "source": 0,
            "target": 3,
        }

    def test_equal_front_points_both_returned_simple(self):
        # simple 模式：等权但拓扑不同的路径作为 equal_paths 一起返回
        r = run_request({**self._req(), "mode": "simple"})
        self.assertEqual(len(r["pareto_front"]), 1)
        item = r["pareto_front"][0]
        self.assertEqual((item["time"], item["cost"]), (2, 4))
        self.assertEqual(item["equal_path_count"], 2)
        routes = [tuple(item["nodes"])] + [
            tuple(p["nodes"]) for p in item["equal_paths"]
        ]
        self.assertIn((0, 1, 3), routes)
        self.assertIn((0, 2, 3), routes)
        # 每条替代路径都必须独立通过重建校验
        self.assertTrue(r["statistics"]["paths_verification_ok"])

    def test_equal_front_points_walk_mode_dedup(self):
        # walk 模式：目标相等视为重复标签，只返回一个 Pareto 点
        r = run_request({**self._req(), "mode": "walk"})
        self.assertEqual(
            [(p["time"], p["cost"]) for p in r["pareto_front"]], [(2, 4)]
        )
        self.assertNotIn("equal_paths", r["pareto_front"][0])

    def test_near_equal_costs_merge_within_tolerance(self):
        # 两条路径 time 相同，cost 相差 1e-12：
        # atol=1e-9 时视为等权，两条都通过 equal_paths 返回；
        # atol=0 时严格区分，稍贵的路径被支配。
        req = {
            "graph": {
                "nodes": ["a", "b", "c"],
                "edges": [
                    {"source": "a", "target": "b", "time": 1.0, "cost": 1.0},
                    {"source": "a", "target": "c", "time": 2.0, "cost": 1.5},
                    {"source": "b", "target": "c",
                     "time": 1.0, "cost": 0.500000000001},
                ],
            },
            "source": "a",
            "target": "c",
            "rtol": 0.0,
        }
        r = run_request({**req, "atol": 1e-9})
        self.assertEqual(len(r["pareto_front"]), 1)
        item = r["pareto_front"][0]
        self.assertEqual(item["equal_path_count"], 2)
        routes = [tuple(item["nodes"])] + [
            tuple(q["nodes"]) for q in item["equal_paths"]
        ]
        self.assertIn(("a", "c"), routes)
        self.assertIn(("a", "b", "c"), routes)

        r_strict = run_request({**req, "atol": 0.0})
        self.assertEqual(len(r_strict["pareto_front"]), 1)
        self.assertNotIn(
            "equal_paths", r_strict["pareto_front"][0]
        )


class TestTruncation(unittest.TestCase):
    def _dense_request(self):
        # 一个会产生大量非支配标签的图：层状图，每层多条边
        edges = []
        layers = 3
        width = 5
        nodes = ["s"]
        for li in range(layers):
            nodes += [f"l{li}_{k}" for k in range(width)]
        nodes.append("t")

        # s -> 第 0 层
        for k in range(width):
            edges.append(
                {"source": "s", "target": f"l0_{k}",
                 "time": k, "cost": width - k}
            )
        # 层间全连接，权重各异，制造大量非支配组合
        for li in range(layers - 1):
            for a in range(width):
                for b in range(width):
                    edges.append(
                        {"source": f"l{li}_{a}", "target": f"l{li+1}_{b}",
                         "time": a + b, "cost": 2 * width - a - b}
                    )
        for k in range(width):
            edges.append(
                {"source": f"l{layers-1}_{k}", "target": "t",
                 "time": k, "cost": width - k}
            )
        return {
            "graph": {"nodes": nodes, "edges": edges},
            "source": "s",
            "target": "t",
        }

    def test_node_label_cap_reports_truncation(self):
        req = self._dense_request()
        req["mode"] = "simple"
        req["node_label_cap"] = 5
        req["total_label_cap"] = 100000
        r = run_request(req)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["status"], "truncated")
        reasons = {t["reason"] for t in r["truncation"]}
        self.assertIn("node_label_cap", reasons)
        for t in r["truncation"]:
            self.assertIn("node", t)
            self.assertGreaterEqual(t["dropped_labels"], 1)

    def test_total_label_cap_reports_truncation(self):
        req = self._dense_request()
        req["mode"] = "simple"
        req["node_label_cap"] = 100000
        req["total_label_cap"] = 30
        r = run_request(req)
        self.assertTrue(r["truncated"])
        reasons = {t["reason"] for t in r["truncation"]}
        self.assertIn("total_label_cap", reasons)

    def test_no_truncation_when_caps_generous(self):
        r = run_request(base_request())
        self.assertFalse(r["truncated"])
        self.assertEqual(r["truncation"], [])


if __name__ == "__main__":
    unittest.main()
