#!/usr/bin/env python3
"""端到端验收（全部真实执行，不用任何静态 JSON 充当证据）。

流程：
  阶段0  随机 ROS_DOMAIN_ID 隔离 + 后台启动 FastAPI 采集服务
  阶段1  可靠性不兼容：listener=reliable / talker=best_effort
         => API 判定 INCOMPATIBLE(R-REL-001)；listener 实际收消息数必须为 0
         修正 talker=reliable 重启 => 不兼容项消除（verdict compatible/risk*）；收消息数必须开始增长
  阶段2  持久性不兼容：listener=transient_local / talker=volatile
         => API 判定 INCOMPATIBLE(R-DUR-001)；收消息数必须为 0
         修正 talker=transient_local 重启 => 不兼容项消除；收消息数必须增长
         (*实时 DDS 发现不携带 history/depth，整体 verdict 可能为 risk(R-UNKNOWN-001)，
           但可靠性/持久性规则必须恢复 ok，且不得有任何 incompatible)
  阶段3  快照：创建/读取成功；篡改一个字节后必须被 SHA-256/HMAC 拒绝

任何一步失败立即非零退出并打印原因。
"""

from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
PORT = random.randint(20000, 30000)
DOMAIN = str(random.randint(40, 90))
BASE = f"http://127.0.0.1:{PORT}"

GREEN, RED, YELLOW, END = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def log(msg: str) -> None:
    print(f"{YELLOW}[accept]{END} {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"{GREEN}[  OK  ]{END} {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"{RED}[ FAIL ]{END} {msg}", flush=True)
    sys.exit(1)


def api(path: str, method: str = "GET"):
    req = urllib.request.Request(BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def api_status(path: str, method: str = "POST") -> int:
    req = urllib.request.Request(BASE + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def read_status(path: Path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"received": 0}


class Process:
    def __init__(self, name: str, cmd: list[str], env_extra: dict[str, str], workdir: Path):
        self.name = name
        log_file = workdir / f"{name}.log"
        self.log_fh = log_file.open("w")
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.update(env_extra)
        self.proc = subprocess.Popen(
            cmd, cwd=workdir, env=env, stdout=self.log_fh, stderr=subprocess.STDOUT
        )

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.log_fh.close()


def wait_diagnose(topic: str, want: str, timeout: float = 20.0):
    """等待话题达到目标状态。

    want='incompatible'：verdict 精确为 incompatible；
    want='healthy'：无 incompatible 且 REL/DUR 类规则全部 ok（兼容恢复；
                    实时 DDS 不传播 history/depth，整体 verdict 可能是 risk(R-UNKNOWN-001)）。
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            data = api(f"/api/diagnose?topic={urllib.parse.quote(topic)}&include_system=false")
            topics = data.get("topics", [])
            if topics:
                td = topics[0]
                verdict = td["verdict"]
                last = verdict
                if want == "incompatible" and verdict == "incompatible":
                    return td
                if want == "healthy" and verdict in ("compatible", "risk"):
                    rules = rule_severities(td)
                    if not any(s == "incompatible" for _r, s in rules):
                        return td
        except (urllib.error.URLError, urllib.error.HTTPError):
            pass
        time.sleep(0.5)
    fail(f"话题 {topic} 在 {timeout}s 内未达到 {want}（最后状态={last}）")


def rule_severities(topic_diag: dict) -> list[tuple[str, str]]:
    out = []
    for pair in topic_diag.get("pairs", []):
        for f in pair["findings"]:
            out.append((f["rule_id"], f["severity"]))
    return out


def wait_count(status_file: Path, minimum: int, timeout: float) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        n = read_status(status_file).get("received", 0)
        if n >= minimum:
            return n
        time.sleep(0.3)
    return read_status(status_file).get("received", 0)


def assert_count_stays_zero(status_file: Path, hold: float) -> None:
    time.sleep(hold)
    n = read_status(status_file).get("received", 0)
    if n != 0:
        fail(f"不兼容链路下 listener 竟然收到 {n} 条消息（期望 0，物理证据与规则判定矛盾）")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="qosdiag-accept-"))
    data_dir = workdir / "data"
    log(f"工作目录 {workdir}，ROS_DOMAIN_ID={DOMAIN}，HTTP 端口={PORT}")

    # 前置项目根，但必须保留 ROS 注入的 PYTHONPATH（/opt/ros/.../site-packages）
    env_common = {
        "ROS_DOMAIN_ID": DOMAIN,
        "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    procs: list[Process] = []
    try:
        # ---------- 阶段0：启动服务 ----------
        server = Process(
            "server",
            [PYTHON, "-m", "qos_diag.cli", "serve", "--port", str(PORT),
             "--data-dir", str(data_dir), "--interval", "0.5", "--grace", "3.0"],
            env_common, ROOT,
        )
        procs.append(server)
        for _ in range(40):
            try:
                if api("/health")["status"] == "ok":
                    break
            except urllib.error.URLError:
                time.sleep(0.5)
        else:
            fail("HTTP 服务未在 20s 内就绪（见 server.log）")
        ok("服务健康检查通过")

        topic_r = "/qos_accept/reliability"
        topic_d = "/qos_accept/durability"

        # ---------- 阶段1：reliability 不兼容 ----------
        log("阶段1：listener=reliable, talker=best_effort（期望 INCOMPATIBLE / R-REL-001）")
        sf1 = workdir / "listener1.json"
        listener1 = Process("listener_bad_rel", [
            PYTHON, str(ROOT / "scripts/qos_listener.py"), "--topic", topic_r,
            "--reliability", "reliable", "--durability", "volatile",
            "--history", "keep_last", "--depth", "10", "--status-file", str(sf1),
        ], env_common, workdir)
        procs.append(listener1)
        talker_be = Process("talker_be", [
            PYTHON, str(ROOT / "scripts/qos_talker.py"), "--topic", topic_r,
            "--reliability", "best_effort", "--durability", "volatile",
            "--history", "keep_last", "--depth", "10",
        ], env_common, workdir)
        procs.append(talker_be)

        diag = wait_diagnose(topic_r, "incompatible")
        rules = rule_severities(diag)
        if ("R-REL-001", "incompatible") not in rules:
            fail(f"未发现 R-REL-001 不兼容判定，实际 findings={rules}")
        ok("规则引擎判定 R-REL-001 确定不兼容")
        assert_count_stays_zero(sf1, hold=4.0)
        ok("物理证据：不兼容链路 4s 内 listener 收到 0 条消息（真实 DDS 拒绝建链）")

        # 修正：talker 改为 reliable
        talker_be.stop(); procs.remove(talker_be)
        talker_rel = Process("talker_rel", [
            PYTHON, str(ROOT / "scripts/qos_talker.py"), "--topic", topic_r,
            "--reliability", "reliable", "--durability", "volatile",
            "--history", "keep_last", "--depth", "10",
        ], env_common, workdir)
        procs.append(talker_rel)
        diag = wait_diagnose(topic_r, "healthy")
        if not any(r[0] == "R-REL-001" and r[1] == "ok" for r in rule_severities(diag)):
            fail("修正后 R-REL-001 未显示通过")
        n = wait_count(sf1, 1, timeout=15.0)
        if n < 1:
            fail("修正后 listener 仍未收到消息")
        ok(f"可靠性修复：verdict={diag['verdict']}（无任何不兼容项），listener 实际收到 {n} 条消息")

        listener1.stop(); procs.remove(listener1)
        talker_rel.stop(); procs.remove(talker_rel)

        # ---------- 阶段2：durability 不兼容 ----------
        log("阶段2：listener=transient_local, talker=volatile（期望 INCOMPATIBLE / R-DUR-001）")
        sf2 = workdir / "listener2.json"
        listener2 = Process("listener_bad_dur", [
            PYTHON, str(ROOT / "scripts/qos_listener.py"), "--topic", topic_d,
            "--reliability", "reliable", "--durability", "transient_local",
            "--history", "keep_last", "--depth", "10", "--status-file", str(sf2),
        ], env_common, workdir)
        procs.append(listener2)
        talker_vol = Process("talker_vol", [
            PYTHON, str(ROOT / "scripts/qos_talker.py"), "--topic", topic_d,
            "--reliability", "reliable", "--durability", "volatile",
            "--history", "keep_last", "--depth", "10",
        ], env_common, workdir)
        procs.append(talker_vol)

        diag = wait_diagnose(topic_d, "incompatible")
        rules = rule_severities(diag)
        if ("R-DUR-001", "incompatible") not in rules:
            fail(f"未发现 R-DUR-001 不兼容判定，实际 findings={rules}")
        ok("规则引擎判定 R-DUR-001 确定不兼容")
        assert_count_stays_zero(sf2, hold=4.0)
        ok("物理证据：持久性不兼容链路 4s 内 listener 收到 0 条消息")

        talker_vol.stop(); procs.remove(talker_vol)
        talker_tl = Process("talker_tl", [
            PYTHON, str(ROOT / "scripts/qos_talker.py"), "--topic", topic_d,
            "--reliability", "reliable", "--durability", "transient_local",
            "--history", "keep_last", "--depth", "10",
        ], env_common, workdir)
        procs.append(talker_tl)
        diag = wait_diagnose(topic_d, "healthy")
        if not any(r[0] == "R-DUR-001" and r[1] == "ok" for r in rule_severities(diag)):
            fail("修正后 R-DUR-001 未显示通过")
        n = wait_count(sf2, 1, timeout=15.0)
        if n < 1:
            fail("持久性修正后 listener 仍未收到消息")
        ok(f"持久性修复：verdict={diag['verdict']}（无任何不兼容项），listener 实际收到 {n} 条消息")

        # ---------- 事件带时间戳（发现/重新发现真实存在） ----------
        events = api(f"/api/events?topic={urllib.parse.quote(topic_d)}&limit=50")
        evs = [e for e in events["events"] if e["event"] == "discovered"]
        if not evs or "timestamp" not in evs[0]:
            fail("未观察到带时间戳的 discovered 事件")
        ok(f"端点发现事件带时间戳（示例 timestamp={evs[0]['timestamp']:.3f}）")

        # ---------- 阶段3：快照完整性 ----------
        log("阶段3：快照创建 / 读取 / 防篡改")
        snap = api("/api/snapshots", method="POST")
        file_name = snap["file"]
        loaded = api(f"/api/snapshots/{file_name}")
        if loaded["rules_version"] != snap["rules_version"]:
            fail("快照规则版本不一致")
        ok(f"快照 {file_name} 创建并通过 SHA-256+HMAC-SHA256 校验，rules_version={snap['rules_version']}")

        snap_path = data_dir / file_name
        raw = json.loads(snap_path.read_text())
        payload = raw["payload"]
        # 篡改 payload 中一个数字字符
        for i, ch in enumerate(payload):
            if ch.isdigit():
                raw["payload"] = payload[:i] + ("0" if ch != "0" else "1") + payload[i + 1:]
                break
        tampered = data_dir / "tampered.json"
        tampered.write_text(json.dumps(raw))
        code = api_status("/api/snapshots/tampered.json", method="GET")
        if code != 409:
            fail(f"篡改快照应返回 409，实际 {code}")
        ok("篡改一个字节后快照加载被拒绝（409，完整性/签名保护生效）")

        print(f"\n{GREEN}========== 全部验收通过 =========={END}")
        print(f"日志与数据保留在: {workdir}")
        return 0
    finally:
        for p in reversed(procs):
            p.stop()


if __name__ == "__main__":
    sys.exit(main())
