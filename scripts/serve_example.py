#!/usr/bin/env python3
"""真实 HTTP 服务调用演示：后台启动 uvicorn，用 httpx 发起请求。

运行：PYTHONPATH=src python3 scripts/serve_example.py
产物：results/service_calls.jsonl（每个请求一行：方法/路径/状态码/响应摘要）。
覆盖：健康检查、模型列表、正常查询、交叉核验、未知模型 404、字母表外符号 422、
空输入 422、预算未完成（200 + complete=false）。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(base: str, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("uvicorn 提前退出")
        try:
            r = httpx.get(base + "/health", timeout=1.0)
            if r.status_code == 200:
                return
        except httpx.TransportError:
            time.sleep(0.2)
    raise RuntimeError("服务在超时内未就绪")


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env_log = RESULTS / "serve"
    env_log.mkdir(exist_ok=True)
    cmd = [
        sys.executable, "-m", "uvicorn", "wfst.service.app:app",
        "--host", "127.0.0.1", "--port", str(port),
    ]
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["WFST_DB_PATH"] = str(RESULTS / "serve.db")
    env["WFST_LOG_DIR"] = str(env_log)
    (RESULTS / "serve.db").unlink(missing_ok=True)

    proc = subprocess.Popen(cmd, env=env, cwd=str(ROOT),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    records = []
    try:
        wait_ready(base, proc)

        def call(name: str, method: str, path: str, **kw):
            r = httpx.request(method, base + path, timeout=10.0, **kw)
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = r.text[:200]
            rec = {"name": name, "method": method, "path": path,
                   "http_status": r.status_code}
            if isinstance(body, dict):
                rec["success"] = body.get("success")
                if body.get("data"):
                    d = body["data"]
                    rec["data_summary"] = {
                        k: d.get(k) for k in
                        ("status", "complete", "agree", "corpus_id", "version")
                        if k in d
                    }
                    if d.get("hypotheses"):
                        rec["hypotheses"] = d["hypotheses"][:5]
                if body.get("error"):
                    rec["error_category"] = body["error"].get("category")
            else:
                rec["body"] = body
            records.append(rec)
            return r, body

        call("health", "GET", "/health")
        call("models", "GET", "/models")
        call("query kat", "POST", "/query", json={
            "corpus_id": "char_morph_demo", "input": "kat", "k": 5})
        call("cross-check citi", "POST", "/query/cross-check", json={
            "corpus_id": "char_morph_demo", "input": "citi", "k": 6})
        call("unknown model", "POST", "/query", json={
            "corpus_id": "ghost", "input": "a"})
        call("unknown symbol", "POST", "/query", json={
            "corpus_id": "char_morph_demo", "input": "qvx"})
        call("empty input", "POST", "/query", json={
            "corpus_id": "char_morph_demo", "input": ""})
        call("budget cut", "POST", "/query", json={
            "corpus_id": "epsilon_ambiguity_demo", "input": "ab",
            "k": 100, "budget": 10})
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    out = RESULTS / "service_calls.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    for rec in records:
        print(f"{rec['http_status']:3} {rec['method']:4} {rec['path']:22} "
              f"success={rec.get('success')} {rec.get('error_category') or ''}")
    print(f"\n已写出：{out}")
    expected = {
        "health": 200, "models": 200, "query kat": 200,
        "cross-check citi": 200, "unknown model": 404,
        "unknown symbol": 422, "empty input": 422, "budget cut": 200,
    }
    ok = all(
        next(r for r in records if r["name"] == name)["http_status"] == code
        for name, code in expected.items()
    )
    budget_rec = next(r for r in records if r["name"] == "budget cut")
    ok = ok and budget_rec["data_summary"]["complete"] is False
    print("HTTP 用例判定：", "ALL_OK" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
