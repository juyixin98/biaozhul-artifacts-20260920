# -*- coding: utf-8 -*-
"""测试/演示用“中继器”：在两条模拟链之间搬运状态证明。

与真实 IBC 中继器一样，它只读取状态、构造证明、提交交易，
不掌握任何链私钥，也无法伪造检查点或 Merkle 证明。
"""
from __future__ import annotations

from typing import Optional

from app import paths
from app.engine import DEFAULT_ACK, Engine


class Relayer:
    def __init__(self, engine: Engine):
        self.engine = engine

    # ---- 基础原语 ----

    def commit(self, chain_id: str, time_nanos: Optional[int] = None) -> dict:
        return self.engine.commit_block(chain_id, time_nanos)

    def proof(self, chain_id: str, key: bytes) -> dict:
        return self.engine.get_proof(chain_id, key.hex())

    # ---- 建链 + 客户端 + 连接 ----

    def setup_two_chains(
        self,
        a: str = "chainA",
        b: str = "chainB",
        genesis_time_nanos: int = 1_000_000_000_000,
        trusting_nanos: int = 24 * 60 * 60 * 1_000_000_000,
    ) -> None:
        self.engine.create_chain(a, revision_number=1, genesis_time_nanos=genesis_time_nanos)
        self.engine.create_chain(b, revision_number=1, genesis_time_nanos=genesis_time_nanos)
        self.engine.create_client(a, b, trusting_period_nanos=trusting_nanos)
        self.engine.create_client(b, a, trusting_period_nanos=trusting_nanos)
        self.engine.create_connection("conn-1", a, b)

    # ---- 通道四次握手（自动提交区块、搬运证明）----

    def handshake(
        self,
        a: str,
        b: str,
        chan_a: str = "channel-0",
        chan_b: str = "channel-0",
        ordering: str = "UNORDERED",
        version: str = "ics20-1",
    ) -> None:
        # 1. Init on A
        self.engine.channel_open_init(a, chan_a, "conn-1", ordering, version, chan_b)
        # 2. TryOpen on B（需 A 的 INIT 证明）
        self.commit(a)
        proof_init = self.proof(a, paths.channel_key(chan_a))
        self.engine.channel_open_try(
            b, chan_b, "conn-1", ordering, version, chan_a, version, proof_init
        )
        # 3. Ack on A（需 B 的 TRYOPEN 证明）
        self.commit(b)
        proof_try = self.proof(b, paths.channel_key(chan_b))
        self.engine.channel_open_ack(a, chan_a, proof_try)
        # 4. Confirm on B（需 A 的 OPEN 证明）
        self.commit(a)
        proof_ack = self.proof(a, paths.channel_key(chan_a))
        self.engine.channel_open_confirm(b, chan_b, proof_ack)
        self.commit(b)

    # ---- 完整包流程 ----

    def send_and_commit(self, src: str, src_chan: str, **send_kwargs) -> dict:
        pkt = self.engine.send_packet(src, src_chan, **send_kwargs)
        self.commit(src)
        return pkt

    def relay_recv(self, pkt: dict, ack_hex: Optional[str] = None) -> dict:
        proof = self.proof(pkt["source_chain"],
                           paths.packet_commitment_key(pkt["source_channel"], pkt["sequence"]))
        out = self.engine.recv_packet(pkt, proof, ack_hex)
        self.commit(pkt["destination_chain"])
        return out

    def relay_ack(self, pkt: dict, ack_hex: Optional[str] = None) -> dict:
        dst = pkt["destination_chain"]
        dst_chan = pkt["destination_channel"]
        seq = pkt["sequence"]
        ack = ack_hex or DEFAULT_ACK.hex()
        proofs = {
            "proof_ack": self.proof(dst, paths.packet_ack_key(dst_chan, seq)),
            "proof_receipt": self.proof(dst, paths.packet_receipt_key(dst_chan, seq)),
            "proof_next_seq_recv": self.proof(dst, paths.next_seq_recv_key(dst_chan)),
        }
        out = self.engine.acknowledge_packet(pkt, ack, proofs)
        self.commit(pkt["source_chain"])
        return out

    def timeout_proofs(self, pkt: dict, *, closed: bool = False) -> dict:
        dst = pkt["destination_chain"]
        dst_chan = pkt["destination_channel"]
        seq = pkt["sequence"]
        if closed:
            return {"proof_channel_closed": self.proof(dst, paths.channel_key(dst_chan))}
        return {
            "proof_unreceived": self.proof(dst, paths.packet_receipt_key(dst_chan, seq))
            if _channel_is_unordered(self.engine, dst, dst_chan)
            else self.proof(dst, paths.next_seq_recv_key(dst_chan)),
        }


def _channel_is_unordered(engine: Engine, chain_id: str, channel_id: str) -> bool:
    return engine.channel_info(chain_id, channel_id)["ordering"] == "UNORDERED"
