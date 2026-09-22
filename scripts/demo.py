"""端到端演示：对运行中的服务执行建链→注册→冻结→双签→查询全流程。

用法（先启动服务）:
    python scripts/demo.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
EX = Path(__file__).resolve().parent.parent / "examples"


def post(client: httpx.Client, path: str, json_body=None) -> dict:
    r = client.post(BASE + path, json=json_body or {})
    r.raise_for_status()
    return r.json()


def put(client: httpx.Client, path: str, json_body) -> dict:
    r = client.put(BASE + path, json=json_body)
    r.raise_for_status()
    return r.json()


def wait_ready(client: httpx.Client, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get(BASE + "/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise SystemExit("服务未就绪，请先运行: uvicorn app.main:app --port 8000")


def load(name: str) -> dict:
    return json.loads((EX / name).read_text(encoding="utf-8"))


def main() -> None:
    chain = load("chain.json")
    keys = load("keys.json")["validators"]

    with httpx.Client(timeout=10, trust_env=False) as c:
        wait_ready(c)
        print("== 建链 / 注册验证者 / 设置当前权益 ==")
        post(c, "/api/v1/chains", chain)
        for name, k in keys.items():
            post(c, f"/api/v1/chains/{chain['chain_id']}/validators",
                 {"validator_pubkey": k["pubkey_hex"], "moniker": name})
            put(c, f"/api/v1/chains/{chain['chain_id']}/validators/{k['pubkey_hex']}/power",
                {"power": k["power"]})

        print("== epoch 0 边界冻结快照 ==")
        print(post(c, f"/api/v1/chains/{chain['chain_id']}/epochs/0/freeze"))

        print("== round 7 第一张票 (blockA) ==")
        print(post(c, f"/api/v1/chains/{chain['chain_id']}/votes",
                   load("vote_round7_blockA.json"))["result"])

        print("== 完全相同的重复票 -> duplicate，不构成双签 ==")
        print(post(c, f"/api/v1/chains/{chain['chain_id']}/votes",
                   load("vote_round7_blockA_dup.json"))["result"])

        print("== round 7 第二张冲突票 (blockB) -> new_evidence 并处罚 ==")
        out = post(c, f"/api/v1/chains/{chain['chain_id']}/votes",
                   load("vote_round7_blockB.json"))
        print(json.dumps(out["penalty"], indent=2, ensure_ascii=False))
        eid = out["evidence_id"]

        print("== 迟到的重复/冲突票只归档，不二次处罚 ==")
        print(post(c, f"/api/v1/chains/{chain['chain_id']}/votes",
                   load("vote_round7_blockA.json"))["result"])

        print("== 证据详情（原文 + 判定版本 + 惩罚引用快照）==")
        detail = c.get(BASE + f"/api/v1/evidences/{eid}").json()
        print("judge_version:", detail["evidence"]["judge_version"])
        print("status:", detail["evidence"]["status"])
        print("penalty:", json.dumps(detail["penalty"], ensure_ascii=False))

        print("== round 10 (epoch 1) 双签：快照未冻结，处罚挂起 ==")
        post(c, f"/api/v1/chains/{chain['chain_id']}/votes", load("vote_round10_blockA.json"))
        out2 = post(c, f"/api/v1/chains/{chain['chain_id']}/votes",
                    load("vote_round10_blockC.json"))
        print(json.dumps(out2["penalty"], ensure_ascii=False))

        print("== 冻结 epoch 1 后自动恢复，处罚引用 epoch 1 快照 ==")
        print(post(c, f"/api/v1/chains/{chain['chain_id']}/epochs/1/freeze")["recovery"])


if __name__ == "__main__":
    sys.exit(main())
