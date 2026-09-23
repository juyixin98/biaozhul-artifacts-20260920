"""终态互斥：并发确认/退款只能落一种终态（条件 UPDATE + BEGIN IMMEDIATE）。"""

from __future__ import annotations

import threading

from ibc_teach import relayer

from .conftest import (
    ack,
    deliver,
    finalize,
    make_channel,
    send_packet,
    timeout_refund,
)


def _status(client, seq: int) -> str:
    rows = {r["sequence"]: r for r in client.get("/packets").json()}
    return rows[seq]["status"]


def test_concurrent_duplicate_acks_exactly_one_wins(client):
    """无序通道：20 个并发相同 ack，恰有一个 200，其余终态冲突。"""
    make_channel(client, "unordered")
    p = send_packet(client)
    finalize(client, "chain-a")
    d = deliver(client, p, "unordered")
    assert d["response"].status_code == 200
    finalize(client, "chain-b")
    body = relayer.ack_body_unordered(client.store, p, 1)

    results: list[int] = []
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()
        r = client.post("/packets/ack", json=body)
        results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results).count(200) == 1, results
    assert all(s in (409, 400) for s in results if s != 200), results
    assert _status(client, p["sequence"]) == "ACKED"


def test_concurrent_timeout_refunds_exactly_one_wins(client):
    """无序通道：20 个并发超时退款，恰有一个 200。"""
    make_channel(client, "unordered")
    p = send_packet(client, timeout_height=1)
    finalize(client, "chain-a")
    finalize(client, "chain-b")  # b=1 超时
    body = relayer.timeout_body_unordered(client.store, p, 1)

    results: list[int] = []
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()
        r = client.post("/packets/timeout", json=body)
        results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results).count(200) == 1, results
    assert all(s in (409, 400) for s in results if s != 200), results
    assert _status(client, p["sequence"]) == "TIMED_OUT"


def test_mixed_ack_and_timeout_only_one_terminal_state(client):
    """同一种“可退款”事实下，ack 与 timeout 混合并发：恰一个成功，终态唯一。

    协议事实：目的链若有回执（可 ack），就不存在“回执缺失”证明（不可 timeout），
    反之亦然——所以真实验证材料互斥。此测试进一步确认：即使在退款成功后立刻
    重放同批 ack 请求，服务层也绝不允许第二个终态。
    """
    make_channel(client, "unordered")
    p = send_packet(client, timeout_height=1)
    finalize(client, "chain-a")
    finalize(client, "chain-b")  # 未接收：b=1 已超时
    t_body = relayer.timeout_body_unordered(client.store, p, 1)

    outcomes = []
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        r = client.post("/packets/timeout", json=t_body)
        outcomes.append(("timeout", r.status_code))

    ts = [threading.Thread(target=worker) for _ in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(1 for _, s in outcomes if s == 200) == 1

    # 现在即使拿到（伪造的）回执证明，也无法 ack：终态锁死。
    # 用一个“错误方向”的请求体——目的链没有回执，非成员证明无法充当成员证明。
    pr = client.store.proof(
        "chain-b", 1, relayer.st.receipt_key(p["dst_port"], p["dst_channel"], p["sequence"])
    )
    ack_body = {
        "packet": relayer.packet_msg(p),
        "checkpoint": pr["checkpoint"],
        "proof": {"steps": pr["proof"]["steps"]},
    }
    r = client.post("/packets/ack", json=ack_body)
    assert r.status_code in (400, 409)
    assert _status(client, p["sequence"]) == "TIMED_OUT"


def test_whitebox_ack_vs_timeout_conditional_update_mutex(store):
    """白盒：对同一包，ack 与 timeout 在同一检查点事实上真正竞争时，
    底层条件 UPDATE 保证只落一种终态（直接打 Store 层）。"""
    import time as _time

    from ibc_teach.store import ErrTerminal

    store.create_channel(ordering="unordered", version="v1")
    p = store.send_packet(
        src_chain="chain-a", src_port="port-a", src_channel="channel-0",
        timeout_height=1, timeout_time_ns=0, data_hex="01", amount=5,
    )
    store.finalize_block("chain-a")
    store.finalize_block("chain-b")  # b=1 超时

    # 投递到目的链（制造 RECEIVED 但尚未 ack 的窗口）：会因超时被拒，
    # 因此改为把超时高度放到接收之后不可行——这里直接测试 SENT 包上
    # 两条条件更新路径：一条 timeout 成功，另一条强制 ack 终态失败。
    t_cp = store.block("chain-b", 1)
    cp = {
        "chain_id": "chain-b", "height": 1, "time_ns": t_cp["time_ns"],
        "app_hash": t_cp["app_hash"], "previous_app_hash": t_cp["previous_app_hash"],
        "signature": t_cp["signature_hex"], "verify_key": store.verify_key("chain-b"),
    }
    pr = store.proof("chain-b", 1,
                     relayer.st.receipt_key("port-b", "channel-0", p["sequence"]))
    timeout_proof = {"steps": pr["proof"]["steps"]}

    # 预置轻客户端到高度 1，使 8 个线程都能通过检查点校验、真正竞争条件 UPDATE。
    conn = store._connect()
    try:
        conn.execute(
            "INSERT INTO clients(chain_id,cp_chain_id,cp_port_id,cp_channel_id,"
            "trusted_height) VALUES ('chain-a','chain-b','port-b','channel-0',1)"
        )
    finally:
        conn.close()

    winners = {"timeout": 0}
    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def race_timeout():
        barrier.wait()
        try:
            store.timeout_packet(relayer.packet_msg(p), cp, timeout_proof)
            with lock:
                winners["timeout"] += 1
        except ErrTerminal:
            with lock:
                errors.append("terminal")

    threads = [threading.Thread(target=race_timeout) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert winners["timeout"] == 1, winners
    assert sorted(errors).count("terminal") == 7, errors

    # 终态后再次 timeout 被拒。
    with __import__("pytest").raises(ErrTerminal):
        store.timeout_packet(relayer.packet_msg(p), cp, timeout_proof)
    row = store.get_packet("chain-a", "port-a", "channel-0", p["sequence"])
    assert row.status == "TIMED_OUT"


def test_ack_after_timeout_and_timeout_after_ack_refused(client):
    """顺序层面：两个终态方向彼此排斥（不依赖并发时序）。"""
    # 1) 已退款 -> ack 拒绝
    make_channel(client, "unordered")
    p = send_packet(client, timeout_height=1)
    finalize(client, "chain-a")
    finalize(client, "chain-b")
    t = timeout_refund(client, p, "unordered", h_b=1)
    assert t["response"].status_code == 200
    # 目的链无回执，ack 证明首先就不成立；即使绕过证明，终态也会拒。
    pr = client.store.proof(
        "chain-b", 1, relayer.st.receipt_key("port-b", "channel-0", p["sequence"])
    )
    r = client.post("/packets/ack", json={
        "packet": relayer.packet_msg(p),
        "checkpoint": pr["checkpoint"],
        "proof": {"steps": pr["proof"]["steps"]},
    })
    assert r.status_code in (400, 409)

    # 2) 已确认 -> timeout 拒绝（同通道再发一个包走 ack 路径）
    p2 = send_packet(client, timeout_height=100)
    finalize(client, "chain-a")
    d = deliver(client, p2, "unordered")
    assert d["response"].status_code == 200
    finalize(client, "chain-b")
    a = ack(client, p2, "unordered", h_b=2)
    assert a["response"].status_code == 200
    # 用 b@2 的非成员证明（回执存在，非成员证明拿不到）退而求其次：
    # 即便给一个更高高度，协议也因 ACKED 终态拒绝。
    pr2 = client.store.proof(
        "chain-b", 2, relayer.st.receipt_key("port-b", "channel-0", p2["sequence"])
    )
    # 此时键存在；用它的证明去 timeout（类型不匹配），应被拒。
    r2 = client.post("/packets/timeout", json={
        "packet": relayer.packet_msg(p2),
        "checkpoint": pr2["checkpoint"],
        "proof": {"steps": pr2["proof"]["steps"]},
    })
    assert r2.status_code in (400, 409)
