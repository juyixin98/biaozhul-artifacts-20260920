"""端到端演示：对运行中的服务（或进程内 ASGI）发起真实 HMAC 签名请求。

覆盖场景：
1) 注册机器人 / 充电点 / 任务
2) 局部最便宜但无法返航的机器人被正确拒绝
3) 多机器人最小成本匹配 + 充电点竞争
4) 取消预占后可重新分配
5) 充电点失效：未开始任务重算，运行中任务保留并告警
6) 实测电量低于预测 -> critical 告警
7) 错误签名被拒（真实密码操作）

用法：
    python -m scripts.demo                 # 进程内跑，无需先启动服务
    python -m scripts.demo http://localhost:8000
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

from app import config, crypto


def signed_headers(method: str, path: str, body_obj) -> dict[str, str]:
    body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    return crypto.sign_request(method, path, body)


class DemoClient:
    def __init__(self, client: httpx.AsyncClient):
        self.c = client

    async def call(self, method: str, path: str, body_obj=None,
                   *, sign: bool = True, tamper: bool = False):
        # 签名覆盖的字节与实际发送的字节必须完全一致
        body = (
            json.dumps(body_obj, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")
            if body_obj is not None else b""
        )
        headers = {"content-type": "application/json"}
        if sign:
            headers.update(crypto.sign_request(method, path, body))
            if tamper:
                headers["X-Signature"] = "deadbeef"
        r = await self.c.request(method, path, content=body, headers=headers)
        r.raise_for_status()
        # 校验响应签名（真实密码操作）
        if sign and "X-Response-Signature" in r.headers:
            ok = crypto.verify_signature(
                config.API_SECRET,
                r.content.decode("utf-8"),
                r.headers["X-Response-Signature"],
            )
            assert ok, "response signature mismatch"
        return r.json()


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


async def run(base_url: str | None) -> None:
    if base_url:
        transport = None
        kwargs = {"base_url": base_url, "trust_env": False}
    else:
        # 进程内演示使用独立临时库，避免与已存在的 dispatch.db 状态冲突
        import tempfile
        from app import database
        tmp = tempfile.mkdtemp(prefix="dispatch-demo-")
        config.DB_PATH = os.path.join(tmp, "demo.db")
        database.init_db(config.DB_PATH)
        from app.main import app
        transport = httpx.ASGITransport(app=app)
        kwargs = {"base_url": "http://demo", "transport": transport}

    async with httpx.AsyncClient(**kwargs) as c:
        api = DemoClient(c)

        hr("0) 健康检查（无需签名）")
        r = await c.get("/healthz")
        print(r.json())

        hr("1) 未签名 / 篡改签名必须被拒（真实 HMAC 校验）")
        r_missing = await c.post("/api/robots", json={"id": "X", "x": 0,
                                                      "y": 0, "battery_wh": 1})
        print("no-signature ->", r_missing.status_code)
        try:
            await api.call("POST", "/api/robots",
                           {"id": "X", "x": 0, "y": 0, "battery_wh": 1},
                           tamper=True)
        except httpx.HTTPStatusError as e:
            print("bad-signature ->", e.response.status_code,
                  e.response.json())

        hr("2) 注册资源")
        for rob in [
            # R3 在任务点旁边：去程局部最便宜，但满电也不够返航+安全余量
            {"id": "R1", "x": 0, "y": 0, "battery_wh": 90.0},
            {"id": "R2", "x": 2, "y": 0, "battery_wh": 400.0},
            {"id": "R3", "x": 49, "y": 1, "battery_wh": 15.0},
        ]:
            print(await api.call("POST", "/api/robots", rob))
        for ch in [
            {"id": "C1", "x": 0, "y": 0},
            {"id": "C2", "x": 30, "y": 0},
            {"id": "C3", "x": 40, "y": 0},
        ]:
            print(await api.call("POST", "/api/chargers", ch))
        # T1 在 50m 外、20kg、等待 60s；T2 在 (40,5)
        t1 = {"id": "T1", "x": 50, "y": 0, "payload_kg": 20, "wait_s": 60}
        t2 = {"id": "T2", "x": 40, "y": 5, "payload_kg": 10, "wait_s": 30}
        print(await api.call("POST", "/api/tasks", t1))
        print(await api.call("POST", "/api/tasks", t2))

        hr("3) 最小成本匹配：注意 R3 局部最便宜但无法保留安全余量返航 -> 拒绝")
        d = await api.call("POST", "/api/dispatch",
                           {"task_ids": ["T1", "T2"]})
        print("algorithm:", d["algorithm"])
        for e in d["candidate_edges"]:
            tag = "FEASIBLE" if e["feasible"] else f"REJECT({e['reason']})"
            print(f"  edge {e['task_id']}-{e['robot_id']} via "
                  f"{e['charger_id']}: {tag} "
                  f"total={e['energy']['total_energy_wh'] if e['energy'] else '-'}")
        for a in d["assigned"]:
            print("ASSIGNED:", a["task_id"], "->", a["robot_id"],
                  "charger", a["charger_id"],
                  "reserve", a["reserved_energy_wh"], "Wh")
            assert crypto.verify_receipt(a["receipt"]["fields"],
                                         a["receipt"]["signature"]), \
                "receipt HMAC invalid"
            print("  receipt HMAC verified OK; command simulated =",
                  a["simulated_command"]["simulated"])
        print("unassigned:", d["unassigned"])

        hr("4) 重复接单必须失败（T1 已 assigned）")
        try:
            await api.call("POST", "/api/dispatch", {"task_ids": ["T1"]})
        except httpx.HTTPStatusError as e:
            print("re-dispatch T1 ->", e.response.status_code,
                  e.response.json())

        hr("5) 取消 T2 的预占（释放电量与充电点，任务回 pending）")
        print(await api.call("POST", "/api/tasks/T2/cancel",
                             {"reason": "demo cancel"}))
        print(await api.call("GET", "/api/snapshot"))

        hr("6) T2 重新分配")
        d2 = await api.call("POST", "/api/dispatch", {"task_ids": ["T2"]})
        print("assigned:", [(a["task_id"], a["robot_id"], a["charger_id"])
                            for a in d2["assigned"]])

        hr("7) T1 开始执行，上报低于预测的实测电量 -> critical 告警")
        print(await api.call("POST", "/api/tasks/T1/start"))
        # 由场景 3 输出确认 T1 被分配给哪台机器人，从分配结果取 robot_id
        t1_robot = next(
            a["robot_id"] for a in d["assigned"] if a["task_id"] == "T1"
        )
        # 走完去程 50m；R3 起步 15Wh、满载速率 0.192 -> 预测剩余 ~5.4Wh，
        # 实测只剩 3.0Wh（低于预测且低于安全余量）
        tele = await api.call("POST", f"/api/robots/{t1_robot}/telemetry", {
            "battery_wh": 3.0,
            "x": 50, "y": 0,
            "mission_completed_distance_m": 50.0,
        })
        print(f"(robot={t1_robot})", tele)

        hr("8a) T2 预占的 C3 失效：T2 尚未开始 -> 释放并自动重算到 C1")
        fr = await api.call("POST", "/api/chargers/C3/fail",
                            {"reason": "demo power outage"})
        print("released:", fr["released_for_recompute"])
        print("running at risk:", fr["running_missions_kept_at_risk"])
        if fr["recompute"]:
            print("recompute assigned:",
                  [(a["task_id"], a["robot_id"], a["charger_id"])
                   for a in fr["recompute"]["assigned"]],
                  "unassigned:", fr["recompute"]["unassigned"])

        hr("8b) T1 正在返航的 C2 失效：任务保留，只产生 critical 风险告警")
        fr2 = await api.call("POST", "/api/chargers/C2/fail",
                             {"reason": "demo second outage"})
        print("released:", fr2["released_for_recompute"])
        print("running at risk:",
              [(x["task_id"], round(x["predicted_residual_wh"], 2))
               for x in fr2["running_missions_kept_at_risk"]])

        hr("9) 告警列表")
        alerts = await api.call("GET", "/api/alerts")
        for al in alerts:
            print(f"  [{al['severity']}] {al['kind']}: {al['message']}")

        print("\nDEMO COMPLETE")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run(url))
