#!/usr/bin/env python3
"""端到端完整演示: 真实启动 uvicorn 子进程, 用 HTTP 跑通全部场景。

场景:
  A. 注册输入并绑定不可变快照
  B. 同快照同种子两次运行 -> 逐字节一致; 换种子 -> 结果不同
  C. 发布新标定不改变旧快照运行; 新快照结果不同
  D. 标记可复现 (全部校验通过)
  E. 测试输入被替换 -> input_verification_failed, 修复后恢复
  F. 产物缺失 / 产物被改 -> 先校验再发布, 失败留痕且无索引
  G. 运行中“改参数” -> 不可变对象被数据库拒绝; 并发运行期间发布新标定无影响
  H. 进程崩溃 + 重启 -> running 变 interrupted, 重新尝试成功
  I. 重复执行按内容记录不同尝试, 不覆盖证据
  J. 索引 HMAC 签名可验; 缺失产物不能标记为可复现

用法: python -m demo.run_demo
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PORT = int(os.environ.get("DEMO_PORT", "8765"))
HOME = ROOT / "data"

PASS, FAIL, INFO = "✅", "❌", "👉"
step_no = 0


def step(title: str) -> None:
    global step_no
    step_no += 1
    print(f"\n{'='*70}\n{PASS} 场景 {step_no}: {title}\n{'='*70}")


def show(label: str, value: object) -> None:
    print(f"  {label}: {json.dumps(value, ensure_ascii=False, sort_keys=True)[:300]}")


class Client:
    def __init__(self) -> None:
        self.conn = http.client.HTTPConnection(HOST, PORT, timeout=10)

    def req(self, method: str, path: str, body=None, raw: bytes | None = None,
            headers: dict[str, str] | None = None):
        hdr = headers or {}
        if body is not None:
            data = json.dumps(body).encode()
            hdr.setdefault("content-type", "application/json")
        elif raw is not None:
            data = raw
        else:
            data = None
        self.conn.request(method, path, body=data, headers=hdr)
        resp = self.conn.getresponse()
        payload = resp.read()
        ctype = resp.getheader("content-type", "")
        parsed = json.loads(payload) if "json" in ctype and payload else payload
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, parsed, headers

    def post_file(self, path: str, filename: str, content: bytes,
                  ctype: str = "application/octet-stream", method: str = "POST"):
        boundary = "----demoboundary42"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return self.req(
            method=method,
            path=path,
            raw=body,
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )

    def close(self) -> None:
        self.conn.close()


def wait_for_port(timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("服务未在预期时间内启动")


def start_server() -> subprocess.Popen:
    env = {
        **os.environ,
        "SNAPSHOT_HOME": str(HOME),
        "PYTHONPATH": str(ROOT),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", HOST, "--port", str(PORT)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_for_port()
    return proc


def stop_server(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def poll(c: Client, attempt_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, att, _ = c.req("GET", f"/attempts/{attempt_id}")
        if att["status"] != "running":
            return att
        time.sleep(0.15)
    raise AssertionError(f"{attempt_id} 超时仍在运行")


def must(cond: bool, msg: str) -> None:
    print(f"  {PASS if cond else FAIL} {msg}")
    if not cond:
        raise SystemExit(f"演示断言失败: {msg}")


def main() -> None:
    if HOME.exists():
        shutil.rmtree(HOME)
    print(f"{INFO} 数据目录: {HOME} (干净启动)")
    proc = start_server()
    c = Client()
    try:
        st, health, _ = c.req("GET", "/health")
        must(st == 200 and health["status"] == "ok", f"健康检查 {health}")

        ex = ROOT / "examples"

        # ---- A. 注册输入 + 绑定不可变快照 ----
        step("注册 bag/参数/标定v1,v2/算法, 绑定不可变快照")
        st, bag, _ = c.post_file("/bags", "bag.json", (ex / "bag.json").read_bytes())
        must(st == 201, f"bag 注册成功: {bag['id'][:24]}… 点数={bag['summary']['num_points']}")
        st, params, _ = c.req("POST", "/params", json.loads((ex / "params.json").read_text()))
        must(st == 201, f"参数: {params['id'][:24]}…")
        cals = json.loads((ex / "calibrations.json").read_text())
        algos = json.loads((ex / "algorithms.json").read_text())
        _, cal1, _ = c.req("POST", "/calibrations", cals["v1"])
        _, cal2, _ = c.req("POST", "/calibrations", cals["v2"])
        _, algo1, _ = c.req("POST", "/algorithms", algos["v1"])
        refs = {
            "bag_id": bag["id"], "params_id": params["id"],
            "calibration_id": cal1["id"], "algorithm_id": algo1["id"],
        }
        st, snap1, _ = c.req("POST", "/snapshots", refs)
        must(st == 201, f"快照 S1: {snap1['id'][:24]}…")
        st, snap2, _ = c.req("POST", "/snapshots", {**refs, "calibration_id": cal2["id"]})
        st_same, snap1_again, _ = c.req("POST", "/snapshots", refs)
        must(snap1_again["id"] == snap1["id"], "同一组引用重复创建快照 -> 同一不可变快照 (幂等)")

        st, _, _ = c.req("PUT", f"/snapshots/{snap1['id']}", {})
        must(st == 405, "PUT 快照被协议拒绝 (405)")

        # ---- B. 同快照同种子逐字节一致 ----
        step("同一固定快照 + 同一运行种子: 两次运行逐字节一致; 换种子则不同")
        st, run42, _ = c.req("POST", f"/snapshots/{snap1['id']}/runs?seed=42")
        st, a1, _ = c.req("POST", f"/runs/{run42['id']}/attempts", {})
        st, a2, _ = c.req("POST", f"/runs/{run42['id']}/attempts", {})
        r1 = poll(c, a1["id"]); r2 = poll(c, a2["id"])
        must(r1["status"] == r2["status"] == "succeeded", "尝试 #1 #2 均成功")
        _, res1, h1 = c.req("GET", f"/attempts/{a1['id']}/artifacts/result")
        _, res2, _ = c.req("GET", f"/attempts/{a2['id']}/artifacts/result")
        must(res1 == res2, "两次产物 JSON 完全一致")
        import hashlib
        res_hash = hashlib.sha256(json.dumps(res1, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        must(h1["content-sha256"] == res_hash, f"响应头 Content-SHA256 与产物字节一致: {res_hash[:16]}…")
        show("结果统计", res1["stats"])

        st, run43, _ = c.req("POST", f"/snapshots/{snap1['id']}/runs?seed=43")
        st, a3, _ = c.req("POST", f"/runs/{run43['id']}/attempts", {})
        poll(c, a3["id"])
        _, res3, _ = c.req("GET", f"/attempts/{a3['id']}/artifacts/result")
        must(res1 != res3, "种子 42 与 43 的产物不同 (种子真实驱动抽样)")

        # ---- C. 新标定不影响旧快照 ----
        step("作业只读取固定快照: 发布新标定后, S1 旧运行不变")
        _, cal3, _ = c.req("POST", "/calibrations", {
            "matrix": [[0.0, -1.0], [1.0, 0.0]], "translation": [1.0, 1.0],
            "note": "90 degree, published later",
        })
        st, run42b, _ = c.req("POST", f"/snapshots/{snap1['id']}/runs?seed=42")
        st, a4, _ = c.req("POST", f"/runs/{run42b['id']}/attempts", {})
        poll(c, a4["id"])
        _, res4, _ = c.req("GET", f"/attempts/{a4['id']}/artifacts/result")
        must(res4 == res1, "S1/seed=42 在发布 v3 标定后结果不变 (读的是快照绑定的 v1)")
        st, snap_c3, _ = c.req("POST", "/snapshots", {**refs, "calibration_id": cal3["id"]})
        st, runc3, _ = c.req("POST", f"/snapshots/{snap_c3['id']}/runs?seed=42")
        st, ac3, _ = c.req("POST", f"/runs/{runc3['id']}/attempts", {})
        poll(c, ac3["id"])
        _, resc3, _ = c.req("GET", f"/attempts/{ac3['id']}/artifacts/result")
        must(resc3 != res1, "引用新标定的新快照结果不同 (标定真实参与计算)")
        print(f"  {INFO} S1 标定哈希 {snap1['calibration_id'][:20]}… 与 S3 {snap_c3['calibration_id'][:20]}… 不同")

        # ---- D. 标记可复现 ----
        step("全部校验通过 -> 标记为可复现")
        st, v1, _ = c.req("GET", f"/attempts/{a1['id']}/verify")
        must(v1["reproducible"] is True, "验证: 输入哈希、种子、产物、重算、索引签名全部通过")
        for chk in v1["checks"]:
            print(f"    {'✓' if chk['ok'] else '✗'} {chk['name']}: {chk['detail'][:60]}")
        st, m1, _ = c.req("POST", f"/attempts/{a1['id']}/mark-reproducible")
        must(st == 200 and m1["status"] == "marked_reproducible", "尝试 #1 标记为 marked_reproducible")

        # ---- E. 输入被替换 ----
        step("测试输入被替换 -> 输入校验失败; 按哈希修复后恢复")
        bag_digest = bag["id"].split("-", 1)[1]
        evidence_path = HOME / "evidence" / bag_digest[:2] / bag_digest
        original_bytes = evidence_path.read_bytes()
        evil = b'{"bag_id":"swapped","points":[[9.9,9.9],[9.8,9.8]]}'
        evidence_path.write_bytes(evil)
        print(f"  {INFO} 已在磁盘上替换证据字节 (路径不变, 内容变)")
        st, runE, _ = c.req("POST", f"/snapshots/{snap1['id']}/runs?seed=42")
        st, aE, _ = c.req("POST", f"/runs/{runE['id']}/attempts", {})
        fE = poll(c, aE["id"])
        must(fE["status"] == "failed" and fE["error_code"] == "input_verification_failed",
             f"输入校验失败, 未运行: {fE['error_code']}")
        st, repair, _ = c.post_file(f"/evidence/{bag_digest}", "bag.json", original_bytes, method="PUT")
        must(st == 200 and repair["repaired"] is True, "用原始内容按哈希修复证据")
        st, bad_repair, _ = c.post_file(f"/evidence/{bag_digest}", "evil.json", evil, method="PUT")
        must(st == 422, "拒绝写入哈希不符的“修复”内容 (不能借修复篡改身份)")
        st, runE2, _ = c.req("POST", f"/snapshots/{snap1['id']}/runs?seed=42")
        st, aE2, _ = c.req("POST", f"/runs/{runE2['id']}/attempts", {})
        must(poll(c, aE2["id"])["status"] == "succeeded", "修复后再次运行成功")

        # ---- F. 产物缺失 / 产物被改: 先校验再发布 ----
        step("产物缺失与产物被改: 先校验再发布, 失败留错误证据、无索引")
        st, runF, _ = c.req("POST", f"/snapshots/{snap1['id']}/runs?seed=77")
        st, aMiss, _ = c.req("POST", f"/runs/{runF['id']}/attempts", {"fault": "lose_artifact"})
        fMiss = poll(c, aMiss["id"])
        must(fMiss["status"] == "failed" and fMiss["error_code"] == "artifact_missing",
             "产物缺失 -> failed/artifact_missing, 无索引")
        must(fMiss["index_entry_id"] is None, "失败尝试没有发布索引")
        st, err_body, _ = c.req("GET", f"/attempts/{aMiss['id']}/artifacts/error")
        must(st == 200 and err_body["error_code"] == "artifact_missing", "失败原因作为错误证据保存")

        st, aCorrupt, _ = c.req("POST", f"/runs/{runF['id']}/attempts", {"fault": "corrupt_result"})
        fCorrupt = poll(c, aCorrupt["id"])
        must(fCorrupt["status"] == "failed" and fCorrupt["error_code"] == "output_verification_failed",
             "产物被运行期改动 -> 重算比对不一致, 拒绝发布")
        st, mark409, _ = c.req("POST", f"/attempts/{aMiss['id']}/mark-reproducible")
        must(st == 409, "缺产物的尝试不能标记为可复现 (409 + 逐项原因)")
        failed_checks = [x["name"] for x in mark409["detail"]["checks"] if not x["ok"]]
        print(f"  {INFO} 未通过的检查项: {failed_checks}")

        # ---- G. 运行中改参数/标定 ----
        step("运行中改参数: 不可变对象在数据库层被拒绝; 运行期间发布新标定不影响在跑的作业")
        import sqlite3
        db = sqlite3.connect(HOME / "snapshots.db")
        denied = False
        try:
            db.execute("UPDATE params SET body_json='{}' WHERE id=?", (params["id"],))
            db.commit()
        except sqlite3.IntegrityError:
            denied = True
            db.rollback()
        must(denied, "直接 UPDATE 参数表被 SQLite 触发器拒绝 (immutable)")
        try:
            db.execute("DELETE FROM calibrations WHERE id=?", (cal1["id"],))
            db.commit()
            denied2 = False
        except sqlite3.IntegrityError:
            denied2 = True
            db.rollback()
        must(denied2, "DELETE 标定同样被拒绝")
        db.close()

        st, runG, _ = c.req("POST", f"/snapshots/{snap1['id']}/runs?seed=555")
        st, aG1, _ = c.req("POST", f"/runs/{runG['id']}/attempts", {"delay_ms": 1200})
        st, aG2, _ = c.req("POST", f"/runs/{runG['id']}/attempts", {"delay_ms": 1200})
        time.sleep(0.3)
        c.req("POST", "/calibrations", {
            "matrix": [[0.9, 0.0], [0.0, 1.1]], "translation": [0.0, 0.0],
            "note": "published while attempts are running",
        })
        print(f"  {INFO} 两个尝试运行期间又发布了一份新标定 (新内容, 不影响在跑作业)")
        g1, g2 = poll(c, aG1["id"]), poll(c, aG2["id"])
        must(g1["status"] == g2["status"] == "succeeded", "并发两个尝试均成功")
        _, rg1, _ = c.req("GET", f"/attempts/{aG1['id']}/artifacts/result")
        _, rg2, _ = c.req("GET", f"/attempts/{aG2['id']}/artifacts/result")
        must(rg1 == rg2, "并发同种子结果一致, 且与标定发布无关")

        # ---- I. 不覆盖证据 ----
        step("重复执行按内容记录不同尝试, 但不覆盖证据")
        st, runI, _ = c.req("POST", f"/snapshots/{snap1['id']}/runs?seed=42")
        must(runI["id"] == run42["id"], "(快照,种子) 相同 -> 同一个 run")
        _, run_view, _ = c.req("GET", f"/runs/{runI['id']}")
        print(f"  {INFO} run 下的尝试编号: {[x['attempt_no'] for x in run_view['attempts']]}")
        must(len(run_view["attempts"]) >= 3, "重复执行累积为多次不同尝试记录, 没有覆盖")

        # ---- H. 进程崩溃 + 重启 ----
        step("运行中进程崩溃 + 重启: running 恢复为 interrupted, 重新尝试成功")
        st, runH, _ = c.req("POST", f"/snapshots/{snap1['id']}/runs?seed=888")
        st, aH, _ = c.req("POST", f"/runs/{runH['id']}/attempts", {"delay_ms": 4000, "fault": "crash"})
        print(f"  {INFO} 尝试 {aH['id'][:16]}… 运行中, 4s 延迟内触发真实进程崩溃…")
        rc = proc.wait(timeout=10)
        must(rc == 2, f"uvicorn 子进程以退出码 {rc} 崩溃 (真实 os._exit)")
        c.close()
        proc = start_server()
        c = Client()
        st, recovered_att, _ = c.req("GET", f"/attempts/{aH['id']}")
        must(recovered_att["status"] == "interrupted",
             f"重启后旧尝试为 {recovered_att['status']}")
        must(recovered_att["error_code"] == "interrupted_on_restart", "中断原因如实记录")
        st, rec, _ = c.req("POST", "/admin/recover")
        must(rec["recovered"] == 0, "再次重启不会重复处理 (幂等恢复)")
        st, aH2, _ = c.req("POST", f"/runs/{runH['id']}/attempts", {})
        must(poll(c, aH2["id"])["status"] == "succeeded", "中断后重新尝试成功")

        # ---- J. 产物被外部删除: 索引在但证据缺失 -> 不可复现 ----
        step("产物证据缺失: 索引与签名仍在, 但不能标记为可复现")
        artifact_id = g1["result_artifact_id"]
        art_digest = artifact_id.split("-", 1)[1]
        art_path = HOME / "evidence" / art_digest[:2] / art_digest
        art_path.unlink()
        print(f"  {INFO} 已从磁盘删除产物证据 {art_digest[:16]}…")
        st, vg, _ = c.req("GET", f"/attempts/{aG1['id']}/verify")
        must(vg["reproducible"] is False, "验证失败: 证据缺失, 尽管数据库记录完好")
        st, mg, _ = c.req("POST", f"/attempts/{aG1['id']}/mark-reproducible")
        must(st == 409, "标记可复现被拒绝")
        st, rep, _ = c.post_file(f"/evidence/{art_digest}", "result.json",
                                 json.dumps(rg1, sort_keys=True, separators=(",", ":"),
                                            ensure_ascii=False).encode(),
                                 method="PUT")
        must(st == 200 and rep["sha256"] == art_digest,
             f"用已发布产物的内容字节按哈希恢复证据 (size={rep['size_bytes']})")
        st, vg2, _ = c.req("GET", f"/attempts/{aG1['id']}/verify")
        must(vg2["reproducible"] is True, "修复后验证恢复通过 (内容寻址, 身份不变)")

        print(f"\n{'='*70}\n{PASS} 全部 10 个场景演示完成, 所有断言通过。\n{'='*70}")
        print(f"  数据保留在: {HOME}\n"
              f"  OpenAPI 文档: http://{HOST}:{PORT}/docs (服务仍在运行, Ctrl+C 脚本不会停服)")
    finally:
        stop_server(proc)
        c.close()


if __name__ == "__main__":
    main()
