"""测试夹具：自动起两条 anvil（测试专用端口），部署 HTLC，测试结束清理。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.chain import LegClient, load_abi, make_hash_lock, packed_preimage  # noqa: E402
from app.config import ANVIL_TEST_KEYS, role_keys  # noqa: E402

ANVIL = os.environ.get("ANVIL_BIN", str(Path.home() / ".foundry" / "bin" / "anvil"))
ALPHA_PORT = 18645
BETA_PORT = 18646


def _wait_rpc(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
    ).encode()
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}", data=payload,
                headers={"content-type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=1) as r:
                if b"result" in r.read():
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"anvil on {port} 未就绪")


@dataclass
class Chains:
    procs: dict
    clients: dict[str, LegClient]
    keys: dict[str, dict[str, str]]
    preimage: bytes
    hash_lock: bytes

    def now(self, leg: str) -> int:
        return self.clients[leg].now()

    def lock_both(self, seed: str, beta_ttl: int, delta: int,
                  amount: int = 10**15, same_hash: bool = True) -> tuple[bytes, int, int]:
        import os
        sid = bytes.fromhex(f"{seed.encode().hex():0>64}"[-64:])
        c = self.clients
        now = max(c["alpha"].now(), c["beta"].now()) + 1
        t_b, t_a = now + beta_ttl, now + beta_ttl + delta
        h_b = self.hash_lock if same_hash else make_hash_lock(os.urandom(32))
        c["alpha"].lock(self.keys["alpha"]["sender"], sid,
                        c["alpha"].account(self.keys["alpha"]["receiver"]).address,
                        self.hash_lock, t_a, amount)
        c["beta"].lock(self.keys["beta"]["sender"], sid,
                       c["beta"].account(self.keys["beta"]["receiver"]).address,
                       h_b, t_b, amount)
        return sid, t_a, t_b

    def lock_one(self, leg: str, seed: str, ttl: int, amount: int = 10**15) -> bytes:
        c = self.clients[leg]
        sid = bytes.fromhex(f"{seed.encode().hex():0>64}"[-64:])
        timelock = c.now() + ttl
        c.lock(self.keys[leg]["sender"], sid,
               c.account(self.keys[leg]["receiver"]).address,
               self.hash_lock, timelock, amount)
        return sid

    def lock_custom_timelocks(self, seed: str, t_b: int, t_a: int,
                              amount: int = 10**15) -> bytes:
        """两侧锁定但时间锁自定义（用于测 tB>=tA 误配置）。"""
        c = self.clients
        sid = bytes.fromhex(f"{seed.encode().hex():0>64}"[-64:])
        c["alpha"].lock(self.keys["alpha"]["sender"], sid,
                        c["alpha"].account(self.keys["alpha"]["receiver"]).address,
                        self.hash_lock, t_a, amount)
        c["beta"].lock(self.keys["beta"]["sender"], sid,
                       c["beta"].account(self.keys["beta"]["receiver"]).address,
                       self.hash_lock, t_b, amount)
        return sid

    def warp_next_tx(self, leg: str, timestamp: int) -> None:
        """把下一笔交易所在区块的时间戳钉死为 timestamp（不单独出空块）。

        anvil 中 setNextBlockTimestamp 只作用于"下一个区块"；若先 evm_mine，
        随后的交易就会落到再下一个块（时间戳至少 +1），无法测 t-1/t 边界。
        """
        self.clients[leg].w3.provider.make_request(
            "evm_setNextBlockTimestamp", [timestamp]
        )

    def pause(self, leg: str) -> None:
        self.procs[leg].send_signal(signal.SIGSTOP)
        time.sleep(0.5)

    def resume(self, leg: str) -> None:
        self.procs[leg].send_signal(signal.SIGCONT)
        time.sleep(0.5)

    def receiver_addr(self, leg: str) -> str:
        return self.clients[leg].account(self.keys[leg]["receiver"]).address


@pytest.fixture(scope="session")
def chains(tmp_path_factory) -> Chains:
    tmp = tmp_path_factory.mktemp("anvil")
    procs = {}
    cfgs = [
        ("alpha", ALPHA_PORT, 31337),
        ("beta", BETA_PORT, 31338),
    ]
    for name, port, chain_id in cfgs:
        state_file = tmp / f"{name}.json"
        procs[name] = subprocess.Popen(
            [
                ANVIL, "--silent", "--host", "127.0.0.1", "--port", str(port),
                "--chain-id", str(chain_id), "--state", str(state_file),
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    try:
        for _, port, _ in cfgs:
            _wait_rpc(port)
        abi = load_abi(ROOT / "out/HTLC.sol/HTLC.json")
        clients = {}
        for name, port, chain_id in cfgs:
            addr, block = LegClient.deploy(
                f"http://127.0.0.1:{port}", chain_id, ANVIL_TEST_KEYS[0],
                ROOT / "out/HTLC.sol/HTLC.json",
            )
            # 短超时：暂停链的用例可以快速失败
            clients[name] = LegClient(
                name, f"http://127.0.0.1:{port}", chain_id, addr, block, abi,
                request_timeout=1.5,
            )
        preimage = packed_preimage("0x" + "ab" * 32)
        ctx = Chains(procs, clients, role_keys(), preimage, make_hash_lock(preimage))
        yield ctx
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
