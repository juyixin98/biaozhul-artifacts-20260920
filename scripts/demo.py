#!/usr/bin/env python3
"""End-to-end acceptance demo for the Robot Experiment Snapshot Service.

It starts a REAL uvicorn server as a subprocess (so the "restart" scenario is a
real process restart), then walks through every requirement:

  1. Upload a synthetic bag + publish a coordinate calibration.
  2. Freeze an immutable, Ed25519-signed snapshot (bag summary + params +
     calibration + algorithm version).
  3. Run two reproducible jobs with recorded input digest and seed.
  4. Publish a NEW calibration and prove it does NOT change existing job results.
  5. Re-run the same content: a new attempt with its own evidence directory
     (no overwrites), byte-identical output.
  6. Validate-then-publish an index, then independently verify the index.
  7. Fault: replace the test input bag  -> revalidation detects it.
  8. Fault: delete an output artifact   -> publication is refused.
  9. Fault: parameters change mid-run   -> run is not reproducible, refused.
 10. Fault: job running during a REAL process crash/restart -> recovered as
     failed/non-reproducible; signing key persists; published index still verifies.

Usage:
    python scripts/demo.py
    # or via the wrapper: ./scripts/demo.sh
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data_demo"
HOST = "127.0.0.1"
PORT = int(os.environ.get("DEMO_PORT", "8765"))
BASE = f"http://{HOST}:{PORT}"
# Never route localhost test traffic through an ambient (e.g. SOCKS) proxy.
CLIENT = httpx.Client(base_url=BASE, trust_env=False, timeout=10.0)

GREEN = "\033[32m"; RED = "\033[31m"; CYAN = "\033[36m"; BOLD = "\033[1m"; END = "\033[0m"
results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))
    tag = f"{GREEN}PASS{END}" if ok else f"{RED}FAIL{END}"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))


def step(title: str) -> None:
    print(f"\n{BOLD}{CYAN}== {title} =={END}")


def wait_for_server(timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=1.0):
                r = CLIENT.get("/health")
                if r.status_code == 200:
                    return
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    raise RuntimeError("server did not become reachable on "
                       f"{HOST}:{PORT} within {timeout}s")


def start_server() -> subprocess.Popen:
    env = {**os.environ, "SNAPSHOT_ROOT": str(DATA), "PORT": str(PORT), "HOST": HOST}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", HOST, "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    try:
        wait_for_server()
    except Exception:
        # Don't leak the server process if readiness polling failed.
        stop_server(proc)
        raise
    return proc


def stop_server(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001
            pass


def crash_server(proc: subprocess.Popen) -> None:
    """Hard kill (SIGKILL) mid-request to simulate a real crash."""
    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    proc.wait(timeout=10)


def main() -> int:
    import shutil
    # Bypass any ambient SOCKS/HTTP proxy for localhost test traffic.
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    if DATA.exists():
        shutil.rmtree(DATA)

    print(f"{BOLD}Robot Experiment Snapshot — end-to-end demo{END}")
    print(f"data dir: {DATA}")

    proc = start_server()
    try:
        # ---- 1. bag + calibration -----------------------------------------
        step("1. Upload synthetic bag and publish coordinate calibration")
        bag_bytes = (ROOT / "examples" / "bag_alpha.csv").read_bytes()
        summary = json.loads((ROOT / "examples" / "bag_summary.json").read_text())
        r = CLIENT.post(
            f"{BASE}/bags",
            files={"file": ("bag_alpha.csv", bag_bytes, "text/csv")},
            data={"summary": json.dumps(summary)},
            timeout=10,
        )
        r.raise_for_status()
        bag_digest = r.json()["digest"]
        print(f"    bag sha256 = {bag_digest[:16]}… ({r.json()['size']} bytes)")

        calib = json.loads((ROOT / "examples" / "calibration.json").read_text())
        calib_id = CLIENT.post(f"{BASE}/calibrations", json=calib).json()["id"]
        print(f"    calibration id = {calib_id[:16]}…")
        check("bag + calibration registered", bool(bag_digest) and bool(calib_id))

        # ---- 2. snapshot ----------------------------------------------------
        step("2. Freeze immutable signed snapshot")
        params = json.loads((ROOT / "examples" / "params.json").read_text())
        snap = CLIENT.post(f"{BASE}/snapshots", json={
            "bag_digest": bag_digest, "params": params,
            "calibration_id": calib_id,
            "algorithm_name": "pointcloud-transform",
            "algorithm_version": "3.2.1",
        }).json()
        sid = snap["id"]
        v = CLIENT.get(f"{BASE}/snapshots/{sid}/verify").json()
        check("snapshot created with 64-hex content id", len(sid) == 64)
        check("Ed25519 snapshot signature verifies", v["signature_valid"])
        check("id == sha256(canonical manifest)", v["id_matches_manifest"])

        # ---- 3. reproducible runs ------------------------------------------
        step("3. Run reproducible jobs (input digest + seed recorded)")
        j1 = CLIENT.post(f"{BASE}/jobs/run-sync",
                        json={"snapshot_id": sid, "seed": 42}).json()
        j2 = CLIENT.post(f"{BASE}/jobs/run-sync",
                        json={"snapshot_id": sid, "seed": 42}).json()
        check("job 1 completed & reproducible",
              j1["status"] == "completed" and j1["reproducible"],
              f"failures={j1['reproducibility_failures']}")
        check("input digest + seed recorded",
              bool(j1["input_digest"]) and j1["seed"] == 42)
        check("repeated run = separate attempt & separate evidence",
              j2["attempt_index"] == 2 and j1["output_path"] != j2["output_path"])
        check("repeated run does NOT overwrite: byte-identical content",
              j1["output_digest"] == j2["output_digest"])

        # ---- 4. new calibration does not change results ---------------------
        step("4. Publish a NEW calibration — existing snapshot results unchanged")
        calib_v2 = json.loads(json.dumps(calib))
        calib_v2["transform"][0][3] = 0.09  # changed translation
        v2_id = CLIENT.post(f"{BASE}/calibrations", json=calib_v2).json()["id"]
        check("new calibration has a new id", v2_id != calib_id)
        j3 = CLIENT.post(f"{BASE}/jobs/run-sync",
                        json={"snapshot_id": sid, "seed": 42}).json()
        check("job still reads pinned calibration; output unchanged",
              j3["output_digest"] == j1["output_digest"],
              f"{j3['output_digest'][:12]} vs {j1['output_digest'][:12]}")

        # ---- 5. validate then publish index ---------------------------------
        step("5. Validate outputs, then publish signed index")
        idx = CLIENT.post(f"{BASE}/indexes", json={"job_ids": [j1["id"], j2["id"]]})
        check("index published after validation", idx.status_code == 201, idx.text[:120])
        idx_body = idx.json()
        iv = CLIENT.get(f"{BASE}/indexes/{idx_body['id']}/verify").json()
        check("published index independently verifies", iv["ok"], str(iv.get("problems")))
        check("index file on disk is append-only #0001",
              os.path.basename(idx_body["path"]) == "index-0001.json")

        # ---- 6. tampered input ----------------------------------------------
        step("6. Fault: replace test input bag on disk → revalidation fails")
        bag_path = DATA / "evidence" / "bags" / f"{bag_digest}.bin"
        held = bag_path.with_suffix(".bin.held")
        os.replace(bag_path, held)
        bag_path.write_bytes(b"0.0,0.0,0.0,1.0\n9.9,9.9,9.9,1.0\n")
        rev = CLIENT.post(f"{BASE}/jobs/{j1['id']}/revalidate").json()
        check("input replacement detected (digest/rerun mismatch)",
              not rev["reproducible"] and
              {"input_bag_digest_mismatch", "nondeterministic_rerun_result"}
              & set(rev["reproducibility_failures"]),
              str(rev["reproducibility_failures"]))
        # Restore the genuine evidence for later steps.
        os.replace(held, bag_path)
        rev = CLIENT.post(f"{BASE}/jobs/{j1['id']}/revalidate").json()
        check("restoring genuine evidence makes it reproducible again",
              rev["reproducible"], str(rev["reproducibility_failures"]))

        # ---- 7. missing output blocks publication ---------------------------
        step("7. Fault: output artifact missing → publication refused")
        jm = CLIENT.post(f"{BASE}/jobs/run-sync",
                        json={"snapshot_id": sid, "seed": 77}).json()
        CLIENT.post(f"{BASE}/jobs/{jm['id']}/simulate-tamper?mode=missing")
        before = len(CLIENT.get(f"{BASE}/indexes").json())
        r = CLIENT.post(f"{BASE}/indexes", json={"job_ids": [jm["id"]]})
        after = len(CLIENT.get(f"{BASE}/indexes").json())
        check("missing-artifact job refused (409)",
              r.status_code == 409 and "output_artifact_missing" in r.text)
        check("no index written on failed validation", before == after)

        # ---- 8. swapped output ----------------------------------------------
        step("8. Fault: output evidence swapped → digest mismatch, original kept")
        js = CLIENT.post(f"{BASE}/jobs/run-sync",
                        json={"snapshot_id": sid, "seed": 88}).json()
        out = CLIENT.post(f"{BASE}/jobs/{js['id']}/simulate-tamper?mode=swap").json()
        check("swapped output detected",
              "output_digest_mismatch" in out["reproducibility_failures"])
        kept = Path(out["output_path"]).parent / "result.json.original-evidence"
        check("original evidence preserved (never overwritten)", kept.exists())

        # ---- 9. parameter change during run ---------------------------------
        step("9. Fault: parameters changed during run → not reproducible")
        jp = CLIENT.post(f"{BASE}/jobs/run-sync",
                        json={"snapshot_id": sid, "seed": 5,
                              "simulate_param_change": True}).json()
        check("param-change run is completed but non-reproducible",
              jp["status"] == "completed" and not jp["reproducible"])
        check("failure reasons recorded",
              "parameters_changed_during_run" in jp["reproducibility_failures"])
        r = CLIENT.post(f"{BASE}/indexes", json={"job_ids": [jp["id"]]})
        check("non-reproducible run cannot enter an index", r.status_code == 409)

        # ---- 10. crash during running job, then restart ---------------------
        step("10. Fault: job running during a REAL process crash → restart recovery")
        jr = CLIENT.post(f"{BASE}/jobs",
                        json={"snapshot_id": sid, "seed": 999, "hold_sec": 30}).json()
        running_id = jr["id"]
        check("job observed running", jr["status"] == "running")
        time.sleep(0.8)
        crash_server(proc)
        proc = None
        time.sleep(0.5)

        proc = start_server()
        health = CLIENT.get(f"{BASE}/health").json()
        check("restart recovered the interrupted job",
              running_id in health["recovered_at_boot"], str(health))
        rec = CLIENT.get(f"{BASE}/jobs/{running_id}").json()
        check("recovered job is failed and non-reproducible",
              rec["status"] == "failed" and not rec["reproducible"],
              str(rec["reproducibility_failures"]))
        r = CLIENT.post(f"{BASE}/indexes", json={"job_ids": [running_id]})
        check("recovered job cannot be published", r.status_code == 409)
        iv = CLIENT.get(f"{BASE}/indexes/{idx_body['id']}/verify").json()
        check("signing key persisted; previously published index still verifies",
              iv["ok"], str(iv.get("problems")))

    finally:
        if proc is not None:
            stop_server(proc)

    # ---- summary -----------------------------------------------------------
    print(f"\n{BOLD}================= DEMO SUMMARY ================={END}")
    failed = [r for r in results if not r[0]]
    for ok, name, detail in results:
        tag = f"{GREEN}PASS{END}" if ok else f"{RED}FAIL{END}"
        print(f"  [{tag}] {name}" + (f" — {detail}" if detail and not ok else ""))
    print(f"\n{GREEN if not failed else RED}{len(results) - len(failed)}/{len(results)} checks passed{END}")
    if failed:
        print(f"\nServer log (if any) was emitted above. Evidence dir: {DATA}")
        return 1
    print(f"\nAll acceptance scenarios demonstrated. Evidence dir: {DATA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
