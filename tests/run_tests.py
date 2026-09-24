#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
End-to-end tests for the shmring SPSC shared-memory queue.

Every test spawns TWO REAL OS PROCESSES (shmring-producer / shmring-consumer)
communicating only through a POSIX shm object in /dev/shm. Nothing runs
in-process; the point of the suite is the cross-process / crash-recovery path.

Tests:
  01 lock-free atomics on this platform
  02 basic FIFO + variable-length messages + empty lines (stdin)
  03 high-frequency wrap-around, content & sequence integrity
  04 full queue: non-blocking reject (ERR_FULL=1) and blocking timeout (3)
  05 producer SIGKILL mid-write -> torn slot recovered, tail unchanged,
     no committed message lost, successor producer resumes
  06 consumer SIGKILL in ack window -> redelivery (at-least-once)
  07 restart fixture: kill producer, restart producer, kill+restart consumer
  08 corruption detection: damaged payload -> ERR_CORRUPT, doctor flags it
  09 oversized message rejected (ERR_TOO_LARGE=4)
  10 role lease: a second live producer is blocked by the robust mutex
  11 message limits: zero-length messages and max_msg boundary messages
"""
import os
import signal
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROD = os.path.join(ROOT, "shmring-producer")
CONS = os.path.join(ROOT, "shmring-consumer")
CTL = os.path.join(ROOT, "shmring-ctl")

PASS, FAIL = 0, 0
FAILURES = []


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {msg}")
    else:
        FAIL += 1
        FAILURES.append(msg)
        print(f"  FAIL {msg}")


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def Popen(args, **kw):
    return subprocess.Popen(args, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, **kw)


def ctl(name, *args):
    return run([CTL, *args, name] if args and args[0] != "check"
               else [CTL, "check"])


def info_kv(name):
    r = run([CTL, "info", name])
    kv = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    return kv


def destroy(name):
    run([CTL, "destroy", name])


def expect_payload(seq, size):
    """Mirror of cli::gen_payload."""
    fill = 0x30 + (seq % 26)
    fill = chr(fill)
    prefix = f"gen:{seq}:{size}:"
    body_len = size - len(prefix)
    if body_len < 0:
        return prefix[:size]
    return prefix + fill * body_len


def parse_msgs(text):
    out = []
    for line in text.splitlines():
        if not line.startswith("MSG seq="):
            continue
        parts = line.split(" data=", 1)
        hdr = dict(kv.split("=", 1) for kv in parts[0].split()[1:])
        out.append((int(hdr["seq"]), int(hdr["len"]),
                    parts[1] if len(parts) > 1 else ""))
    return out


def json_unescape(s):
    out = bytearray()
    i = 0
    while i < len(s):
        c = s[i]
        if c != "\\":
            out.extend(c.encode())
            i += 1
            continue
        e = s[i + 1]
        if e == "u":
            out.append(int(s[i + 2:i + 6], 16))
            i += 6
        else:
            out.append({"n": ord("\n"), "r": ord("\r"), "t": ord("\t"),
                        '"': ord('"'), "\\": ord("\\")}[e])
            i += 2
    return bytes(out)


def fresh_name(tag):
    return f"t{tag}_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------- tests ----

def test_01_lockfree():
    print("test 01: lock-free process-shared atomics")
    r = run([CTL, "check"])
    check(r.returncode == 0, "ctl check exits 0")
    check("NO" not in r.stdout, "both atomic widths lock-free")


def test_02_basic():
    print("test 02: basic FIFO, variable length, empty lines, real processes")
    name = fresh_name("02")
    destroy(name)
    try:
        r = run([CTL, "create", name, "--capacity", "8", "--max-msg", "4096"])
        check(r.returncode == 0, "queue created")
        cons = Popen([CONS, name, "--capacity", "8", "--max-msg", "4096",
                      "--count", "6", "--timeout", "8000"])
        time.sleep(0.2)
        with open(os.path.join(ROOT, "examples", "input.txt"), "rb") as f:
            data = f.read()
        lines = data.split(b"\n")[:6]
        r = run([PROD, name, "--capacity", "8", "--max-msg", "4096",
                 "--stdin", "--timeout", "8000"],
                input=data.decode(), )
        check(r.returncode == 0, f"producer exit 0 (got {r.returncode})")
        out, err = cons.communicate(timeout=10)
        check(cons.returncode == 0, "consumer exit 0")
        msgs = parse_msgs(out)
        check(len(msgs) == 6, f"6 messages delivered (got {len(msgs)})")
        want = lines
        ok = all(json_unescape(m[2]) == want[i] and m[0] == i
                 for i, m in enumerate(msgs))
        check(ok, "FIFO order, exact bytes incl. empty line and unicode")
        kv = info_kv(name)
        check(kv["head"] == kv["tail"] == "6", "head==tail==6 after drain")
    finally:
        destroy(name)


def test_03_wrap():
    print("test 03: high-frequency wrap-around (cap=16, 20000 msgs)")
    name = fresh_name("03")
    destroy(name)
    CAP, SIZE, N = 16, 48, 20000
    try:
        r = run([CTL, "create", name, "--capacity", str(CAP),
                 "--max-msg", "128"])
        check(r.returncode == 0, "queue created")
        cons = Popen([CONS, name, "--capacity", str(CAP), "--max-msg", "128",
                      "--count", str(N), "--timeout", "30000", "--quiet"])
        time.sleep(0.2)
        t0 = time.time()
        r = run([PROD, name, "--capacity", str(CAP), "--max-msg", "128",
                 "--gen", str(N), "--gen-size", str(SIZE), "--timeout", "30000",
                 "--quiet"])
        dt = time.time() - t0
        check(r.returncode == 0, f"producer delivered {N} ({r.returncode})")
        out, err = cons.communicate(timeout=40)
        check(cons.returncode == 0, "consumer got all messages")
        # integrity verified independently below with a second run that prints
        seqs = []
        for line in err.splitlines():
            pass
        kv = info_kv(name)
        wraps = N // CAP
        check(kv["tail"] == str(N) and kv["head"] == str(N),
              f"head==tail=={N} after wrap x{wraps}")
        print(f"      throughput ~{N / dt:,.0f} msg/s ({dt:.2f}s)")

        # content-integrity run (smaller, printable, verified byte-for-byte).
        # stdout goes to a file: a PIPE could fill and block the consumer.
        destroy(name)
        N2 = 5000
        run([CTL, "create", name, "--capacity", str(CAP),
             "--max-msg", "128"])
        outf = open("/tmp/shmring_t03.out", "w")
        cons = subprocess.Popen(
            [CONS, name, "--capacity", str(CAP), "--max-msg", "128",
             "--count", str(N2), "--timeout", "60000"],
            stdout=outf, stderr=subprocess.PIPE, text=True)
        time.sleep(0.1)
        run([PROD, name, "--capacity", str(CAP), "--max-msg", "128",
             "--gen", str(N2), "--gen-size", str(SIZE), "--timeout", "60000",
             "--quiet"])
        cons.wait(timeout=70)
        outf.close()
        msgs = parse_msgs(open("/tmp/shmring_t03.out").read())
        mism = [i for i, m in enumerate(msgs)
                if m[0] != i or json_unescape(m[2]).decode()
                != expect_payload(i, SIZE)]
        check(cons.returncode == 0, "content consumer exited 0")
        check(len(msgs) == N2 and not mism,
              f"all {N2} seq + payload bytes verified across wraps "
              f"(got {len(msgs)} msgs, first bad idx={mism[:1]})")
    finally:
        destroy(name)


def test_04_full():
    print("test 04: full queue — reject policy and blocking timeout")
    name = fresh_name("04")
    destroy(name)
    try:
        run([CTL, "create", name, "--capacity", "4", "--max-msg", "64"])
        # fill with no consumer; non-blocking enqueue must be rejected
        r = run([PROD, name, "--capacity", "4", "--max-msg", "64",
                 "--gen", "8", "--gen-size", "16", "--timeout", "0",
                 "--quiet"])
        check(r.returncode == 1, f"non-blocking enqueue -> ERR_FULL=1 "
                                 f"(got {r.returncode})")
        kv = info_kv(name)
        check(kv["tail"] == "4", "exactly capacity=4 committed, none lost")

        # blocking producer with no consumer must time out with code 3
        r = run([PROD, name, "--capacity", "4", "--max-msg", "64",
                 "--gen", "8", "--gen-size", "16", "--timeout", "300",
                 "--quiet"])
        check(r.returncode == 3, f"blocking enqueue -> ERR_TIMEOUT=3 "
                                 f"(got {r.returncode})")
        kv = info_kv(name)
        check(kv["tail"] == "4", "timeout published nothing extra")

        # drain 2 then a blocking producer makes progress within deadline
        cons = Popen([CONS, name, "--capacity", "4", "--max-msg", "64",
                      "--count", "2", "--timeout", "5000", "--quiet"])
        out, _ = cons.communicate(timeout=8)
        check(cons.returncode == 0, "consumer drained 2")
        r = run([PROD, name, "--capacity", "4", "--max-msg", "64",
                 "--gen", "2", "--gen-size", "16", "--timeout", "5000",
                 "--quiet"])
        check(r.returncode == 0, "producer made progress after space freed")
        kv = info_kv(name)
        check(kv["tail"] == "6" and kv["head"] == "2",
              "tail=6 head=2 (2 new slots reused across wrap indices)")
    finally:
        destroy(name)


def test_05_producer_kill():
    print("test 05: producer SIGKILL mid-write -> torn slot recovery")
    name = fresh_name("05")
    destroy(name)
    CAP = 8
    try:
        run([CTL, "create", name, "--capacity", str(CAP),
             "--max-msg", "256"])
        # 4 committed, then the 5th enqueue copies 10 bytes and SIGKILLs.
        r = run([PROD, name, "--capacity", str(CAP), "--max-msg", "256",
                 "--gen", "5", "--gen-size", "128", "--timeout", "5000",
                 "--quiet", "--kill-at", "4", "--kill-after-bytes", "10"])
        check(r.returncode == -signal.SIGKILL,
              f"producer killed by SIGKILL (rc={r.returncode})")
        kv = info_kv(name)
        check(kv["tail"] == "4" and kv["head"] == "0",
              "tail stays 4: half-written message never committed")
        check(kv["torn_writes"] == "0",
              "torn counter increments only after recovery")

        # consumer drains the 4 confirmed messages
        cons = Popen([CONS, name, "--capacity", str(CAP), "--max-msg", "256",
                      "--count", "4", "--timeout", "5000"])
        out, _ = cons.communicate(timeout=8)
        check(cons.returncode == 0, "all 4 committed messages still readable")
        msgs = parse_msgs(out)
        ok = all(m[0] == i for i, m in enumerate(msgs))
        check(ok, "seqs 0..3 delivered intact; nothing silently dropped")

        # successor producer: robust mutex gives EOWNERDEAD, torn slot at
        # index 4 % 8 is reset and reused for the SAME seq 4.
        r = run([PROD, name, "--capacity", str(CAP), "--max-msg", "256",
                 "--gen", "6", "--gen-size", "128", "--timeout", "5000",
                 "--quiet"])
        check(r.returncode == 0, "restarted producer recovered and sent 6")
        kv = info_kv(name)
        check(kv["torn_writes"] == "1", "exactly 1 torn write recorded")
        check(kv["tail"] == "10",
              "tail=10: reused seq 4..9, no gap in sequence space")

        cons = Popen([CONS, name, "--capacity", str(CAP), "--max-msg", "256",
                      "--count", "6", "--timeout", "5000"])
        out, _ = cons.communicate(timeout=8)
        check(cons.returncode == 0, "consumer read the post-recovery batch")
        msgs = parse_msgs(out)
        check([m[0] for m in msgs] == [4, 5, 6, 7, 8, 9],
              "seqs 4..9 delivered (seq4 = recovered slot)")
    finally:
        destroy(name)


def test_06_consumer_kill():
    print("test 06: consumer SIGKILL in ack window -> redelivery")
    name = fresh_name("06")
    destroy(name)
    CAP = 8
    try:
        run([CTL, "create", name, "--capacity", str(CAP),
             "--max-msg", "256"])
        # 3 committed messages sitting in the queue
        r = run([PROD, name, "--capacity", str(CAP), "--max-msg", "256",
                 "--gen", "3", "--gen-size", "64", "--timeout", "5000",
                 "--quiet"])
        check(r.returncode == 0, "3 messages committed")
        # consumer copies seq0 out then dies before acknowledging
        r = run([CONS, name, "--capacity", str(CAP), "--max-msg", "256",
                 "--count", "5", "--timeout", "2000", "--quiet",
                 "--kill-at", "0", "--kill-after-bytes", "32"])
        check(r.returncode == -signal.SIGKILL, "consumer killed by SIGKILL")
        kv = info_kv(name)
        check(kv["head"] == "0", "head unchanged: crash happened pre-ack")

        # restarted consumer: robust recovery flips READING -> COMMITTED and
        # seq0 must be redelivered (at-least-once).
        cons = Popen([CONS, name, "--capacity", str(CAP), "--max-msg", "256",
                      "--count", "3", "--timeout", "5000"])
        out, _ = cons.communicate(timeout=8)
        check(cons.returncode == 0, "restarted consumer drained queue")
        msgs = parse_msgs(out)
        check([m[0] for m in msgs] == [0, 1, 2],
              "seq0 redelivered after crash (at-least-once)")
        check(json_unescape(msgs[0][2]).decode() == expect_payload(0, 64),
              "redelivered payload byte-identical")
    finally:
        destroy(name)


def test_07_restart_fixture():
    print("test 07: full restart fixture (kill/restart both roles)")
    name = fresh_name("07")
    destroy(name)
    CAP, SIZE = 32, 100
    try:
        run([CTL, "create", name, "--capacity", str(CAP),
             "--max-msg", "256"])
        # long-running producer killed mid-write at seq 1000 (high churn)
        p1 = Popen([PROD, name, "--capacity", str(CAP), "--max-msg", "256",
                    "--gen", "100000", "--gen-size", str(SIZE),
                    "--timeout", "30000", "--quiet",
                    "--kill-at", "1000", "--kill-after-bytes", "50"])
        c1 = Popen([CONS, name, "--capacity", str(CAP), "--max-msg", "256",
                    "--count", "200000", "--timeout", "30000", "--quiet"])
        rc = p1.wait(timeout=30)
        check(rc == -signal.SIGKILL, "producer P1 killed mid-write")
        # give consumer a moment to drain to the freeze point
        time.sleep(1.0)
        c1.send_signal(signal.SIGTERM)
        c1.wait(timeout=5)
        kv = info_kv(name)
        t1 = int(kv["tail"]); h1 = int(kv["head"])
        check(t1 == 1000, f"frozen tail=1000 (got {t1})")
        check(h1 <= t1 and t1 - h1 <= CAP, "consumer drained near tail")
        check(kv["torn_writes"] == "0", "torn slot pending, not yet recovered")

        # restart producer: recovery, then continue the stream with a fixed
        # batch of 2000; queue-assigned sequences continue at 1000..2999.
        def wait_torn(expected, deadline):
            while time.time() < deadline:
                if info_kv(name).get("torn_writes") == str(expected):
                    return True
                time.sleep(0.05)
            return False

        remaining0 = 3000 - int(info_kv(name)["head"])
        c2 = Popen([CONS, name, "--capacity", str(CAP), "--max-msg", "256",
                    "--count", str(remaining0), "--timeout", "60000",
                    "--quiet"])
        p2 = Popen([PROD, name, "--capacity", str(CAP), "--max-msg", "256",
                    "--gen", "2000", "--gen-size", str(SIZE),
                    "--timeout", "60000", "--quiet"])
        check(wait_torn(1, time.time() + 10),
              "P2 open recovered exactly 1 torn slot")
        rc2 = p2.wait(timeout=60)
        check(rc2 == 0, f"P2 published 2000 more and exited cleanly (rc={rc2})")
        out, err = c2.communicate(timeout=70)
        check(c2.returncode == 0, "restarted consumer C2 drained to tail")
        kv = info_kv(name)
        check(kv["head"] == kv["tail"] == "3000",
              f"final head==tail==3000 (got h={kv['head']} t={kv['tail']})")
        check(kv["torn_writes"] == "1", "still exactly 1 torn event")
    finally:
        destroy(name)


def test_08_corrupt():
    print("test 08: corruption detection (CRC) and doctor")
    name = fresh_name("08")
    destroy(name)
    try:
        run([CTL, "create", name, "--capacity", "8", "--max-msg", "256"])
        r = run([PROD, name, "--capacity", "8", "--max-msg", "256",
                 "--gen", "3", "--gen-size", "128", "--timeout", "5000",
                 "--quiet"])
        check(r.returncode == 0, "3 messages committed")
        r = run([CTL, "damage", name, "--seq", "1", "--offset", "10",
                 "--value", "255"])
        check(r.returncode == 0, "payload bit flipped in committed slot")
        r = run([CTL, "doctor", name])
        check(r.returncode == 6 and "bad_slots=1" in r.stdout,
              "doctor reports exactly 1 bad slot")

        # consumer receives seq0 fine, then must be stopped by seq1's CRC
        cons = Popen([CONS, name, "--capacity", "8", "--max-msg", "256",
                      "--count", "3", "--timeout", "3000", "--quiet"])
        out, err = cons.communicate(timeout=8)
        check(cons.returncode == 6, "consumer refuses corrupt msg: ERR_CORRUPT=6")
        check("corrupt=1" in err, "corrupt_events counter incremented")
        kv = info_kv(name)
        check(kv["head"] == "1", "queue frozen at head=1, no silent skip/loss")

        # policy: explicit re-init required to continue
        r = run([CTL, "destroy", name])
        check(r.returncode == 0, "explicit destroy for re-initialisation")
        r = run([CTL, "create", name, "--capacity", "8", "--max-msg", "256"])
        check(r.returncode == 0, "fresh queue usable after re-init")
    finally:
        destroy(name)


def test_09_oversize():
    print("test 09: oversized message rejected")
    name = fresh_name("09")
    destroy(name)
    try:
        run([CTL, "create", name, "--capacity", "4", "--max-msg", "32"])
        # --gen rejects gen-size>max_msg at parse time by design; an oversized
        # real message arrives through the stdin path instead.
        r = run([PROD, name, "--capacity", "4", "--max-msg", "32",
                 "--stdin", "--timeout", "0", "--quiet"],
                input="x" * 33 + "\n")
        check(r.returncode == 4, f"ERR_TOO_LARGE=4 (got {r.returncode})")
        kv = info_kv(name)
        check(kv["tail"] == "0", "oversize publish changed nothing")
        r = run([PROD, name, "--capacity", "4", "--max-msg", "32",
                 "--stdin", "--timeout", "0", "--quiet"],
                input="x" * 32 + "\n")
        check(r.returncode == 0, "exact max_msg boundary accepted")
    finally:
        destroy(name)


def test_10_lease():
    print("test 10: single-producer/single-consumer role leases")
    name = fresh_name("10")
    destroy(name)
    try:
        run([CTL, "create", name, "--capacity", "4", "--max-msg", "64"])
        holder = Popen([PROD, name, "--capacity", "4", "--max-msg", "64",
                        "--gen", "100000", "--gen-size", "16",
                        "--timeout", "30000", "--quiet"])
        time.sleep(0.5)
        # second live producer: the robust role mutex is held by a LIVE owner,
        # so pthread_mutex_lock blocks and the process must still be waiting
        # after 3 s (it never reaches enqueue and cannot corrupt state).
        second = Popen([PROD, name, "--capacity", "4", "--max-msg", "64",
                        "--gen", "1", "--gen-size", "16", "--timeout", "1000",
                        "--quiet"])
        time.sleep(3.0)
        check(second.poll() is None,
              "second live producer blocks on the producer role lease")
        second.kill()
        second.wait(timeout=5)

        # A long-running consumer keeps draining so the successor producer is
        # not blocked merely by queue fullness (which is unrelated to leases).
        cons = Popen([CONS, name, "--capacity", "4", "--max-msg", "64",
                      "--count", "200000", "--timeout", "15000", "--quiet"])
        time.sleep(0.5)  # drain the holder's 4 queued messages
        check(int(info_kv(name)["head"]) >= 4, "holder's messages drained")
        holder.send_signal(signal.SIGTERM)
        holder.wait(timeout=5)
        time.sleep(0.3)
        # after holder exits, the lease transfers cleanly (no EOWNERDEAD),
        # and the successor producer publishes while the consumer keeps running
        r = run([PROD, name, "--capacity", "4", "--max-msg", "64",
                 "--gen", "1", "--gen-size", "16", "--timeout", "5000",
                 "--quiet"])
        check(r.returncode == 0, "producer lease transfers after clean exit")
        deadline = time.time() + 5
        while time.time() < deadline:
            if int(info_kv(name)["tail"]) >= 5:
                break
            time.sleep(0.1)
        kv = info_kv(name)
        check(int(kv["tail"]) >= 5, "successor's message committed")
        r = run([CTL, "doctor", name])
        check(r.returncode == 0, "queue healthy under lease contention")
        cons.send_signal(signal.SIGTERM)
        cons.wait(timeout=5)
    finally:
        destroy(name)


def test_11_zero_and_boundary():
    print("test 11: zero-length messages and max_msg-sized messages")
    name = fresh_name("11")
    destroy(name)
    CAP, MAX = 8, 128
    try:
        run([CTL, "create", name, "--capacity", str(CAP),
             "--max-msg", str(MAX)])
        cons = Popen([CONS, name, "--capacity", str(CAP),
                      "--max-msg", str(MAX), "--count", "6",
                      "--timeout", "5000"])
        time.sleep(0.2)
        # 3 empty lines (incl. adjacent empties), then 3 full-size lines
        payload = ("\n\n\n" + ("x" * MAX + "\n") * 3).rstrip("\n")
        r = run([PROD, name, "--capacity", str(CAP),
                 "--max-msg", str(MAX), "--stdin", "--timeout", "5000",
                 "--quiet"], input=payload + "\n")
        check(r.returncode == 0, "producer sent empties + boundary msgs")
        out, _ = cons.communicate(timeout=8)
        check(cons.returncode == 0, "consumer delivered all 6")
        msgs = parse_msgs(out)
        lens = [m[1] for m in msgs]
        check(lens == [0, 0, 0, MAX, MAX, MAX],
              f"lengths 0,0,0,{MAX},{MAX},{MAX} (got {lens})")
    finally:
        destroy(name)


def main():
    for b in (PROD, CONS, CTL):
        if not os.path.exists(b):
            print(f"missing binary {b}; run 'make' first", file=sys.stderr)
            return 2
    tests = [test_01_lockfree, test_02_basic, test_03_wrap, test_04_full,
             test_05_producer_kill, test_06_consumer_kill,
             test_07_restart_fixture, test_08_corrupt, test_09_oversize,
             test_10_lease, test_11_zero_and_boundary]
    t0 = time.time()
    for t in tests:
        t()
    print(f"\n{'-' * 64}")
    print(f"{PASS} passed, {FAIL} failed in {time.time() - t0:.1f}s")
    if FAILURES:
        print("Failed checks:")
        for f in FAILURES:
            print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
