#!/usr/bin/env python
"""端到端演示脚本: 通过真实 HTTP 请求验证信封加密、主密钥轮换与各类故障。

用法:
    python demo.py                # 自动启动 uvicorn 子进程, 默认 127.0.0.1:8000
    python demo.py --base-url http://127.0.0.1:8000   # 对已运行的服务执行

场景:
  1. 加密上传 / 解密下载 (原始字节 + JSON/base64 两种方式)
  2. 查看对象元数据与主密钥版本
  3. 主密钥轮换, 旧对象轮换后仍可解密; 数据块字节不变
  4. 模拟轮换中断 (fail_after), 混合包裹状态下续跑 rewrap
  5. 错误密钥 (删除旧主密钥) -> 409, 响应无明文
  6. 头部 wrapped-DEK 篡改 -> 400, 无明文
  7. 密文截断 / 比特翻转 -> 400, 无明文
"""

from __future__ import annotations

import argparse
import base64
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent


def wait_for_port(host: str, port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"服务在 {timeout}s 内未就绪: {host}:{port}")


def show(title: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {title}")
    if detail:
        for line in detail.rstrip().splitlines():
            print(f"       {line}")


def run(base_url: str) -> int:
    failures = 0

    def check(title: str, cond: bool, detail: str = "") -> bool:
        nonlocal failures
        show(title, cond, detail)
        if not cond:
            failures += 1
        return cond

    with httpx.Client(base_url=base_url, timeout=10) as c:
        print("== 1. 加密上传 / 解密下载 ==")
        data = "信封加密演示数据-0123456789".encode() * 3
        r = c.put("/objects/report.bin", content=data,
                  headers={"content-type": "application/octet-stream"},
                  params={"block_size": 32})
        check("原始字节上传 (201)", r.status_code == 201, f"key_id={r.json()['metadata']['key_id']} "
              f"plaintext_len={r.json()['metadata']['plaintext_len']}")

        r = c.put("/objects/hello", json={"plaintext_b64": base64.b64encode(b"hello world").decode()})
        check("JSON/base64 上传 (201)", r.status_code == 201)

        r = c.get("/objects/report.bin")
        check("下载并解密成功", r.status_code == 200 and r.content == data,
              f"len={len(r.content)}")
        check("hello 解密成功", c.get("/objects/hello").content == b"hello world")

        print("\n== 2. 元数据 / 主密钥版本 ==")
        meta = c.get("/objects/report.bin/metadata").json()
        check("元数据显示 v1 密钥 / 32B 分块",
              meta["key_id"] == "v1" and meta["block_size"] == 32,
              f"salt={meta['salt_hex']} pt_len={meta['plaintext_len']}")
        keys = c.get("/keys").json()["keys"]
        check("初始只有 v1", [k["key_id"] for k in keys] == ["v1"])

        print("\n== 3. 主密钥轮换 (只重包裹 DEK) ==")
        r = c.post("/keys/rotate")
        body = r.json()
        check("轮换到 v2, 重包裹 2 个对象",
              r.status_code == 200 and body["new_key_id"] == "v2" and body["rewrapped"] == 2,
              str(body))
        meta2 = c.get("/objects/report.bin/metadata").json()
        check("轮换后 key_id=v2 且明文不变",
              meta2["key_id"] == "v2" and c.get("/objects/report.bin").content == data)

        print("\n== 4. 模拟轮换中断 + 续跑 ==")
        for i in range(4):
            c.put(f"/objects/mid{i}", content=f"mid-{i}-".encode() * 8, params={"block_size": 16})
        # 当前 6 个对象都在 v2; 轮换到 v3 时中途失败, 按名字排序前 2 个完成
        r = c.post("/keys/rotate", params={"fail_after": 2})
        check("中断注入返回 500 rotation_interrupted",
              r.status_code == 500 and r.json()["error"] == "rotation_interrupted",
              f"new={r.json()['new_key_id']} rewrapped={r.json()['rewrapped']} remaining={r.json()['remaining']}")
        kms = {c.get(f"/objects/mid{i}/metadata").json()["key_id"] for i in range(4)}
        check("中断后 mid 对象处于 v2/v3 混合包裹状态", kms == {"v2", "v3"}, str(sorted(kms)))
        all_readable = all(c.get(f"/objects/mid{i}").content == f"mid-{i}-".encode() * 8
                           for i in range(4))
        check("混合状态下新旧对象全部可解密", all_readable)
        r = c.post("/keys/rewrap")
        # 剩余 4 个 (hello/mid2/mid3/report.bin) 仍由 v2 包裹
        check("续跑 rewrap 完成剩余对象",
              r.status_code == 200 and r.json()["rewrapped"] == 4, str(r.json()))
        check("续跑后 mid 对象全部为当前密钥",
              all(c.get(f"/objects/mid{i}/metadata").json()["key_id"] == "v3" for i in range(4)))

        print("\n== 5. 错误密钥场景 ==")
        # legacy 用当前密钥 (v3) 保存; 然后轮换出 v4, 但 legacy 故意留在 v3
        c.put("/objects/legacy", content=b"TOP SECRET LEGACY")
        legacy_kid = c.get("/objects/legacy/metadata").json()["key_id"]
        c.post("/keys/rotate", params={"fail_after": 0})  # v4 已建立, 无对象重包裹
        # 新对象直接使用当前 v4
        c.put("/objects/modern", content=b"modern data")
        modern_kid = c.get("/objects/modern/metadata").json()["key_id"]
        check("legacy 在旧密钥、modern 在当前密钥",
              (legacy_kid, modern_kid) == ("v3", "v4"), f"{legacy_kid} / {modern_kid}")
        c.delete(f"/keys/{legacy_kid}")  # 删除旧主密钥 v3
        r = c.get("/objects/legacy")
        check("旧密钥被删 -> 409 key_unavailable",
              r.status_code == 409 and r.json()["error"] == "key_unavailable", r.text)
        check("错误响应不泄露明文", b"TOP SECRET" not in r.content)
        check("当前密钥对象仍可解密", c.get("/objects/modern").content == b"modern data")

        print("\n== 6/7. 头部篡改 / 密文截断 / 比特翻转 (通过容器故障注入接口) ==")
        # 这些场景在测试里直接改存储容器; demo 中用独立的故障注入端点完成同样的字节级篡改
        c.put("/objects/victim", content=b"sensitive block data" * 4, params={"block_size": 16})
        r = c.post("/objects/victim/attack/header")
        check("头部 wrapped-DEK 篡改 -> 400",
              r.status_code == 400 and r.json()["error"] == "decrypt_failed", r.text)
        check("篡改失败响应无明文", b"sensitive" not in r.content)

        c.put("/objects/victim2", content=b"sensitive block data" * 4, params={"block_size": 16})
        r = c.post("/objects/victim2/attack/truncate")
        check("密文截断 -> 400",
              r.status_code == 400 and r.json()["error"] in {"decrypt_failed", "format_error"},
              r.json()["error"])

        c.put("/objects/victim3", content=b"sensitive block data" * 4, params={"block_size": 16})
        r = c.post("/objects/victim3/attack/bitflip")
        check("末块比特翻转 -> 400", r.status_code == 400 and
              r.json()["error"] == "decrypt_failed")
        check("翻转失败响应无明文", b"sensitive" not in r.content)

    print()
    if failures:
        print(f"演示完成: {failures} 项失败")
        return 1
    print("演示完成: 全部场景符合预期")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    if args.base_url:
        return run(args.base_url.rstrip("/"))

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", args.host, "--port", str(args.port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        wait_for_port(args.host, args.port)
        return run(f"http://{args.host}:{args.port}")
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        if out.strip():
            print("\n--- 服务器输出 ---")
            print(out.rstrip())


if __name__ == "__main__":
    raise SystemExit(main())
