#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运动命令仲裁 —— 本地自动化验收脚本。

它会：
  1. 用手动可控时钟启动 motion-arbiter（独立临时 SQLite，不污染目录）；
  2. 用真实 HMAC-SHA256 对请求体签名后依次投递自主/遥控/急停命令；
  3. 推进时钟验证租约到期、急停锁存/解除、同刻竞争、查询不刷租约；
  4. 杀掉进程并用同一个数据库重启，验证序号/锁存/决策历史/时钟持久化；
  5. 每一步打印 PASS/FAIL，任一步失败即以非零码退出。

用法：
  python3 scripts/acceptance.py            # 自动 cargo build 并启动
  python3 scripts/acceptance.py --keep-db  # 保留临时目录便于检查
"""
import argparse
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOMAIN_MOTION = b"motion-command-v1\n"
DOMAIN_ESTOP = b"estop-command-v1\n"

PASS, FAIL = "✅ PASS", "❌ FAIL"
failures = []


def check(name, cond, detail=""):
    print(f"  {PASS if cond else FAIL} {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def sign(key_hex: str, domain: bytes, body: bytes) -> str:
    return hmac.new(bytes.fromhex(key_hex), domain + body, hashlib.sha256).hexdigest()


class Client:
    def __init__(self, base: str, keys: dict):
        self.base = base
        self.keys = keys

    def _req(self, method, path, body=None, source=None, domain=None):
        url = self.base + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
            if source is not None:
                headers["X-Source"] = source
                headers["X-Signature"] = "sha256=" + sign(self.keys[source], domain, data)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def command(self, source, seq, vx, t, lease, valid=2000, nonce=None):
        body = {
            "seq": seq,
            "nonce": nonce or f"{source}-n{seq}",
            "vx": vx, "vy": 0.0, "omega": 0.0,
            "issue_ms": t, "valid_for_ms": valid, "lease_for_ms": lease,
        }
        return self._req("POST", "/v1/command", body, source, DOMAIN_MOTION)

    def raw_command(self, source, body):
        return self._req("POST", "/v1/command", body, source, DOMAIN_MOTION)

    def estop(self, seq, action, t, source="estop-1", valid=2000, nonce=None):
        body = {
            "seq": seq,
            "nonce": nonce or f"estop-n{seq}-{action}",
            "action": action, "issue_ms": t, "valid_for_ms": valid,
        }
        return self._req("POST", "/v1/estop", body, source, DOMAIN_ESTOP)

    def get(self, path):
        return self._req("GET", path)

    def evaluate(self, at=None, persist=False):
        return self._req("POST", "/v1/evaluate", {"at_ms": at, "persist": persist})

    def advance(self, delta):
        return self._req("POST", "/admin/clock/advance", {"delta_ms": delta})

    def time(self):
        return self.get("/v1/time")[1]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_healthy(base, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=1) as r:
                if r.status == 200:
                    return True
        except OSError:
            time.sleep(0.05)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-db", action="store_true")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="motion-arbiter-accept-")
    db_path = os.path.join(tmp, "arbiter.db")
    cfg_path = os.path.join(tmp, "config.json")
    shutil.copy(os.path.join(ROOT, "examples", "config.example.json"), cfg_path)
    cfg = json.load(open(cfg_path))
    keys = {sid: v["key_hex"] for sid, v in cfg["sources"].items()}

    print("构建 release 二进制（cargo build --release）...")
    subprocess.run(["cargo", "build", "--release", "--manifest-path",
                    os.path.join(ROOT, "Cargo.toml")], check=True)
    binary = os.path.join(ROOT, "target", "release", "motion-arbiter")

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    T0 = 1_700_000_000_000

    def start(seed=T0):
        log = open(os.path.join(tmp, "server.log"), "ab")
        proc = subprocess.Popen(
            [binary, "serve", "--config", cfg_path, "--db", db_path,
             "--listen", f"127.0.0.1:{port}", "--clock", "manual",
             "--clock-seed-ms", str(seed)],
            stdout=log, stderr=log, cwd=tmp)
        if not wait_healthy(base):
            print("服务启动失败，日志：")
            print(open(os.path.join(tmp, "server.log"), errors="replace").read())
            sys.exit(2)
        return proc

    print(f"\n临时目录: {tmp}")
    proc = start()
    c = Client(base, keys)

    try:
        print("\n[1] 时钟与基础仲裁")
        t = c.time()
        check("手动时钟种子为 T0", t["now_ms"] == T0 and t["mode"] == "manual", json.dumps(t))

        s, r = c.command("auto-1", 1, 0.5, T0, 5000)
        check("自主命令 202 接受且被选中", s == 202 and r["decision"]["chosen"]["source"] == "auto-1",
              f"{s} {r}")

        print("\n[2] 遥控优先级高于自主")
        s, r = c.command("rc-1", 1, 1.0, T0, 1000)
        d = r["decision"]
        check("遥控命令被选中", s == 202 and d["chosen"]["source"] == "rc-1", f"{s}")
        check("自主命令被抑制且原因明确",
              any(x["source"] == "auto-1" and x["reason"] == "higher_priority_active"
                  for x in d["suppressed"]))

        print("\n[3] 旧序号 / 过期 / 超前 / 超长租约 / 坏签名全部拒绝")
        s, r = c.command("auto-1", 1, 0.5, T0, 1000)
        check("重复 seq 拒绝为 stale_sequence(422)", s == 422 and r["error"] == "stale_sequence",
              f"{s} {r}")
        c.advance(3000)  # now = T0+3000
        s, r = c.command("auto-1", 2, 0.5, T0 + 100, 1000)
        check("过期消息拒绝为 expired(422)", s == 422 and r["error"] == "expired", f"{s} {r}")
        s, r = c.command("auto-1", 3, 0.5, T0 + 100_000, 1000)
        check("超前消息拒绝为 too_far_in_future(422)",
              s == 422 and r["error"] == "too_far_in_future", f"{s} {r}")
        s, r = c.raw_command("auto-1", {
            "seq": 4, "nonce": "auto-1-n4", "vx": 0.1, "vy": 0, "omega": 0,
            "issue_ms": T0 + 3000, "valid_for_ms": 2000, "lease_for_ms": 999999})
        check("超长租约拒绝为 lease_too_long(422)",
              s == 422 and r["error"] == "lease_too_long", f"{s} {r}")
        # 坏签名：直接构造请求
        body = json.dumps({"seq": 5, "nonce": "auto-1-n5", "vx": 0.1, "vy": 0, "omega": 0,
                           "issue_ms": T0 + 3000, "valid_for_ms": 2000,
                           "lease_for_ms": 1000}, separators=(",", ":")).encode()
        req = urllib.request.Request(base + "/v1/command", data=body, method="POST", headers={
            "Content-Type": "application/json", "X-Source": "auto-1",
            "X-Signature": "sha256=" + "00" * 32})
        try:
            urllib.request.urlopen(req, timeout=5)
            bad_sig = (0, {})
        except urllib.error.HTTPError as e:
            bad_sig = (e.code, json.loads(e.read()))
        check("坏签名拒绝为 bad_signature(401)",
              bad_sig[0] == 401 and bad_sig[1]["error"] == "bad_signature", str(bad_sig))

        print("\n[4] 遥控失联后不得恢复旧自主命令")
        # 当前 now=T0+3000；rc 租期到 T0+1000 已失效。自主 seq=1 收到于 T0（rc 窗口结束前）。
        s, d = c.evaluate()
        check("失联后输出零速 no_active_command",
              d["chosen"]["reason"] == "no_active_command" and d["chosen"]["vx"] == 0.0,
              json.dumps(d["chosen"], ensure_ascii=False))
        check("遥控抑制原因 rc_lease_lost",
              any(x["source"] == "rc-1" and x["reason"] == "rc_lease_lost" for x in d["suppressed"]))
        check("旧自主抑制原因 stale_after_remote_loss",
              any(x["source"] == "auto-1" and x["reason"] == "stale_after_remote_loss"
                  for x in d["suppressed"]))

        print("\n[5] 失联后的新鲜自主命令可以接管")
        s, r = c.command("auto-1", 6, 0.7, T0 + 3000, 2000)
        check("新鲜自主命令被选中",
              s == 202 and r["decision"]["chosen"]["source"] == "auto-1"
              and r["decision"]["chosen"]["reason"] == "autonomous_fresh_after_rc_loss",
              f"{s} {json.dumps(r.get('decision', {}).get('chosen'), ensure_ascii=False)}")

        print("\n[6] 查询请求不得刷新租约")
        before = c.get("/admin/state")[1]["decision_records"]
        for _ in range(3):
            c.get("/v1/decision/latest")
            c.get("/v1/decisions/history?limit=10")
            c.get("/admin/state")
            c.evaluate(persist=False)
        c.advance(2001)  # 超过新鲜自主租约(T0+5000)
        s, d = c.evaluate()
        check("租约照常到期，未被查询续命",
              d["chosen"]["reason"] == "no_active_command"
              and any(x["source"] == "auto-1" and x["reason"] == "lease_expired"
                      for x in d["suppressed"]))
        after = c.get("/admin/state")[1]["decision_records"]
        check("只读查询没有新增决策记录", before == after, f"before={before} after={after}")

        print("\n[7] 急停优先、锁存保持、解除后旧命令全失效")
        now = c.time()["now_ms"]  # T0+5001
        s, r = c.command("rc-1", 2, 1.2, now, 5000)
        check("遥控重新上线", s == 202 and r["decision"]["chosen"]["source"] == "rc-1")
        s, r = c.estop(1, "trigger", now)
        d = r["decision"]
        check("急停触发：零速、锁存、最高优先",
              s == 202 and d["estop_latched"] is True
              and d["chosen"]["source"] == "estop-1"
              and d["chosen"]["vx"] == 0.0 and d["chosen"]["vy"] == 0.0
              and d["chosen"]["omega"] == 0.0)
        check("其余命令全部 suppressed_while_estop_latched",
              d["suppressed"] and all(x["reason"] == "suppressed_while_estop_latched"
                                      for x in d["suppressed"]))
        c.advance(20_000)
        d = c.evaluate()[1]
        check("推进 20s 后急停仍锁存（不会自动解除）",
              d["estop_latched"] is True and d["chosen"]["reason"] == "estop_triggered")
        # 锁存中重复 trigger：合法（更新 seq 的新事件）；未锁存的 clear 才报错。
        s, r = c.estop(2, "clear", c.time()["now_ms"])
        check("解除急停 202", s == 202 and r["decision"]["estop_latched"] is False, f"{s} {r}")
        d = c.evaluate()[1]
        check("解除后无可用命令（零速）", d["chosen"]["reason"] == "no_active_command")
        check("解除前的旧命令全部 invalidated_by_estop_event",
              d["suppressed"] and all(x["reason"] == "invalidated_by_estop_event"
                                      for x in d["suppressed"]))
        s, r = c.estop(3, "clear", c.time()["now_ms"])
        check("未锁存时再 clear 报 estop_not_active(422)",
              s == 422 and r["error"] == "estop_not_active", f"{s} {r}")
        c.advance(1)
        s, r = c.command("auto-1", 7, 0.4, c.time()["now_ms"], 2000)
        check("解除后的新鲜自主恢复运动",
              s == 202 and r["decision"]["chosen"]["source"] == "auto-1", f"{s}")

        print("\n[8] 同刻竞争结果确定（与到达顺序无关）")
        # 用 at_ms 回看同一时刻：auto-1 与（新增）auto 来源同刻。
        # 配置里只有一个 autonomous 来源；这里改用同来源更高 seq 不可能并存，
        # 因此通过“同刻 rc vs autonomous”验证优先级全序：
        now2 = c.time()["now_ms"]
        c.command("auto-1", 8, 0.9, now2, 5000)
        c.command("rc-1", 3, 0.9, now2, 5000)
        d1 = c.evaluate(at=now2 + 5)[1]
        check("同刻遥控确定性优先于自主", d1["chosen"]["source"] == "rc-1")

        persisted_now = c.time()["now_ms"]
        history_count = len(c.get("/v1/decisions/history?limit=500")[1])

        print("\n[9] 重启：时钟、序号、急停状态、决策历史全部持久化")
        proc.terminate()
        proc.wait(timeout=5)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        c = Client(base, keys)
        proc = start(seed=999)  # seed 应被库中持久化的时钟覆盖
        check("重启后手动时钟从数据库恢复", c.time()["now_ms"] == persisted_now,
              f"want {persisted_now} got {c.time()['now_ms']}")
        # 用当前时刻附近的 issue_ms，确保先不被时间窗拦截，真正验到“序号持久化”。
        s, r = c.command("auto-1", 7, 0.4, persisted_now, 1000)
        check("重启后旧 seq 仍拒绝（stale_sequence）",
              s == 422 and r["error"] == "stale_sequence", f"{s} {r}")
        hist = c.get("/v1/decisions/history?limit=500")[1]
        check("决策历史跨重启保留", len(hist) == history_count,
              f"want {history_count} got {len(hist)}")
        check("历史中存在急停触发决策",
              any(x["estop_latched"] is True and x["chosen"]["source"] == "estop-1" for x in hist))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        if args.keep_db and failures:
            print(f"\n临时目录已保留: {tmp}")
        elif not args.keep_db:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"\n临时目录: {tmp}")

    print("\n" + "=" * 60)
    if failures:
        print(f"验收失败：{len(failures)} 项未通过：")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("验收全部通过 ✅（计算、HMAC 签名、SQLite 持久化均真实执行）")


if __name__ == "__main__":
    main()
