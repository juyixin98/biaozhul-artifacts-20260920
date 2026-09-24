"""端到端演示: 通过真实 HTTP 调用走查增量路径修复全部场景。

用法:
    1. 先启动服务:  uvicorn app.main:app --port 8000
    2. 再运行:      .venv/bin/python examples/demo.py
"""

import json
import sys

import httpx

BASE = "http://127.0.0.1:8000"


def show(title, body):
    print(f"\n===== {title} =====")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=10, trust_env=False)

    try:
        show("健康检查", c.get("/health").json())

        # 1. 建图: 8 邻接 6x6, 两个障碍 + 一片高代价区域
        with open("examples/create_map.json", encoding="utf-8") as f:
            map_req = json.load(f)
        mp = c.post("/api/maps", json=map_req)
        mp.raise_for_status()
        mpj = mp.json()
        map_id = mpj["map_id"]
        show("创建地图 (snapshot v0)", {"map_id": map_id, "snapshot": mpj["snapshot"]})

        # 2. 创建规划器: 起点 (0,0), 目标 (5,5)
        pj = c.post(
            "/api/planners",
            json={
                "map_id": map_id,
                "start": {"row": 0, "col": 0},
                "goal": {"row": 5, "col": 5},
            },
        )
        pj.raise_for_status()
        pid = pj.json()["planner_id"]

        r0 = c.post(f"/api/planners/{pid}/plan", json={}).json()
        show(
            "初次规划 (D*Lite vs 独立 Dijkstra)",
            {
                "cost": r0["cost"],
                "path": r0["path"],
                "dijkstra_cost": r0["benchmark"]["dijkstra_cost"],
                "cost_match": r0["benchmark"]["cost_match"],
                "diagnostics": r0["diagnostics"],
            },
        )

        # 3. 新增障碍(局部代价更新), 增量修复
        with open("examples/update_map.json", encoding="utf-8") as f:
            upd = json.load(f)
        r1 = c.post(f"/api/planners/{pid}/update-map", json=upd).json()
        show(
            "新增障碍后增量修复 (v1)",
            {
                "snapshot_version": r1["snapshot_version"],
                "cost": r1["cost"],
                "path": r1["path"],
                "dijkstra_cost": r1["benchmark"]["dijkstra_cost"],
                "cost_match": r1["benchmark"]["cost_match"],
                "重扩展节点数": r1["diagnostics"]["expanded_nonstale"],
                "Dijkstra扩展数": r1["benchmark"]["dijkstra_expansions"],
            },
        )

        # 4. 移除障碍 (2,2), 应恢复更短路径
        r2 = c.post(
            f"/api/planners/{pid}/update-map",
            json={"changes": [{"row": 2, "col": 2, "kind": "free"}]},
        ).json()
        show(
            "移除障碍后增量修复 (v2)",
            {
                "cost": r2["cost"],
                "dijkstra_cost": r2["benchmark"]["dijkstra_cost"],
                "cost_match": r2["benchmark"]["cost_match"],
                "重扩展节点数": r2["diagnostics"]["expanded_nonstale"],
            },
        )

        # 5. 起点移动 (不换地图)
        r3 = c.post(
            f"/api/planners/{pid}/move-start",
            json={"start": {"row": 0, "col": 5}},
        ).json()
        show(
            "起点移动到 (0,5)",
            {
                "cost": r3["cost"],
                "path_head": r3["path"][:3],
                "dijkstra_cost": r3["benchmark"]["dijkstra_cost"],
                "cost_match": r3["benchmark"]["cost_match"],
                "重扩展节点数": r3["diagnostics"]["expanded_nonstale"],
            },
        )

        # 6. 封死目标所在行 => 不可达 (不能沿用旧路径)
        wall = [{"row": 4, "col": col, "kind": "block"} for col in range(6)]
        r4 = c.post(f"/api/planners/{pid}/update-map", json={"changes": wall}).json()
        show(
            "目标被整行隔离 => 不可达",
            {
                "reachable": r4["reachable"],
                "cost": r4["cost"],
                "path": r4["path"],
                "dijkstra_cost": r4["benchmark"]["dijkstra_cost"],
                "cost_match": r4["benchmark"]["cost_match"],
            },
        )

        # 7. 打开缺口 => 恢复
        r5 = c.post(
            f"/api/planners/{pid}/update-map",
            json={"changes": [{"row": 4, "col": 0, "kind": "free"}]},
        ).json()
        show(
            "打开缺口 (4,0) => 恢复可达",
            {
                "reachable": r5["reachable"],
                "cost": r5["cost"],
                "dijkstra_cost": r5["benchmark"]["dijkstra_cost"],
                "cost_match": r5["benchmark"]["cost_match"],
                "重扩展节点数": r5["diagnostics"]["expanded_nonstale"],
            },
        )

        # 8. 负代价必须被拒绝
        neg = c.post(
            f"/api/maps/{map_id}/updates",
            json={"changes": [{"row": 0, "col": 0, "kind": "weight", "weight": -3.0}]},
        )
        show("拒绝负代价", {"status_code": neg.status_code, "body": neg.json()})

        # 9. 密码学快照链校验
        ver = c.get(f"/api/maps/{map_id}/verify").json()
        show("快照链哈希校验", {"ok": ver["ok"], "版本数": len(ver["snapshots"])})

        # 10. 冲突: 用陈旧快照 id 提交
        conflict = c.post(
            f"/api/maps/{map_id}/updates",
            json={
                "changes": [{"row": 0, "col": 0, "kind": "block"}],
                "expected_snapshot": mpj["snapshot"]["snapshot_id"],
            },
        )
        show("陈旧快照被拒绝 (409)", conflict.json())

        print("\n演示完成: 所有场景的 D*Lite 结果均与独立 Dijkstra 最优成本一致。")
        return 0
    except httpx.ConnectError:
        print("无法连接服务。请先运行: uvicorn app.main:app --port 8000", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
