import bootstrap  # noqa: F401

import http.client
import io
import json
import logging
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from anti_replay.keys import create_or_update_key
from anti_replay.logutil import RedactingFilter, configure_logging
from anti_replay.server import build_server
from anti_replay.signing import sign_request


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _send(port, method, target, headers, body=b""):
    # 并发突增时内核可能短暂 reset（与应用层去重无关），重试到拿到响应为止。
    last_exc = None
    for _ in range(10):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, target, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, data
        except (ConnectionResetError, http.client.RemoteDisconnected, OSError) as exc:
            last_exc = exc
            time.sleep(0.05)
        finally:
            conn.close()
    raise last_exc  # type: ignore[misc]


def _send_raw(port, method, raw_target, headers, body=b""):
    """用裸 TCP 发送，逐字节保留 request-target（http.client 会折叠 . 段）。"""
    import socket

    lines = [f"{method} {raw_target} HTTP/1.1", "Host: 127.0.0.1"]
    if body:
        headers = {**headers, "Content-Length": str(len(body))}
    for name, value in headers.items():
        lines.append(f"{name}: {value}")
    lines.append("Connection: close")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(raw)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    response = b"".join(chunks)
    status_line = response.split(b"\r\n", 1)[0].decode("ascii", "replace")
    # HTTP/1.1 200 OK
    status = int(status_line.split(" ", 2)[1])
    data = response.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in response else b""
    return status, data


def _signed(key, key_id, target, body, *, method="POST", ts=None, nonce=None):
    ts = str(int(time.time()) if ts is None else ts)
    nonce = nonce or ("nonce-" + os.urandom(9).hex())
    sig, _, _, _ = sign_request(
        key=key, key_id=key_id, method=method, target=target,
        body=body, timestamp=ts, nonce=nonce,
    )
    return {
        "Content-Type": "application/json",
        "X-Auth-Key-Id": key_id,
        "X-Auth-Timestamp": ts,
        "X-Auth-Nonce": nonce,
        "X-Auth-Signature": sig,
    }


def _attacker_signed(key, key_id, cpath, cquery, body, *,
                     method="POST", ts=None, nonce=None):
    """模拟绕过合规客户端的攻击者：直接对给定规范材料（可能是恶意 target）签名。"""
    from anti_replay.canonical import build_canonical_request
    from anti_replay.crypto import hmac_sha256_hex

    ts = str(int(time.time()) if ts is None else ts)
    nonce = nonce or ("nonce-" + os.urandom(9).hex())
    cr = build_canonical_request(
        key_id=key_id, method=method,
        canonical_path_value=cpath, canonical_query_value=cquery,
        body=body, timestamp=ts, nonce=nonce,
    )
    return {
        "Content-Type": "application/json",
        "X-Auth-Key-Id": key_id,
        "X-Auth-Timestamp": ts,
        "X-Auth-Nonce": nonce,
        "X-Auth-Signature": hmac_sha256_hex(key, cr.encode()),
    }


class ServerFixture:
    def __init__(self, *, window=300, store="memory"):
        self.tmp = tempfile.TemporaryDirectory()
        self.keystore = os.path.join(self.tmp.name, "keys.json")
        self.secret = create_or_update_key(self.keystore, "k1")
        self.port = _free_port()
        kwargs = dict(
            keystore_path=self.keystore, host="127.0.0.1", port=self.port,
            window_seconds=window, store=store, log_level="INFO",
        )
        if store == "sqlite":
            kwargs["db_path"] = os.path.join(self.tmp.name, "nonces.db")
        self.server = build_server(**kwargs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # 等服务起来
        for _ in range(50):
            try:
                status, _ = _send(self.port, "GET", "/health", {})
                if status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.02)

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.server.nonce_store.close()
        self.tmp.cleanup()


class HttpEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = ServerFixture()
        cls.port = cls.fx.port
        cls.key = cls.fx.secret

    @classmethod
    def tearDownClass(cls):
        cls.fx.stop()

    def test_health(self):
        status, data = _send(self.port, "GET", "/health", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"status": "ok"})

    def test_001_valid_request(self):
        body = b'{"amount":100}'
        headers = _signed(self.key, "k1", "/api/data", body)
        status, data = _send(self.port, "POST", "/api/data", headers, body)
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertEqual(payload["status"], "accepted")

    def test_002_exact_replay_returns_409(self):
        body = b'{"case":"replay"}'
        headers = _signed(self.key, "k1", "/api/data", body, nonce="nonce-replay-e2e")
        s1, _ = _send(self.port, "POST", "/api/data", headers, body)
        s2, data = _send(self.port, "POST", "/api/data", headers, body)
        self.assertEqual(s1, 200)
        self.assertEqual(s2, 409)
        self.assertEqual(json.loads(data)["error"]["code"], "replay_detected")

    def test_003_concurrent_duplicates_only_one_200(self):
        body = b'{"case":"concurrent"}'
        headers = _signed(self.key, "k1", "/api/data", body, nonce="nonce-conc-e2e1")
        n = 32
        barrier = threading.Barrier(n)
        statuses = []

        def one():
            barrier.wait()
            statuses.append(_send(self.port, "POST", "/api/data", dict(headers), body)[0])

        with ThreadPoolExecutor(max_workers=n) as pool:
            list(pool.map(lambda _: one(), range(n)))
        self.assertEqual(sorted(statuses).count(200), 1)
        self.assertEqual(sorted(statuses).count(409), n - 1)

    def test_004_body_tampering_bad_signature(self):
        body = b'{"amount":100}'
        headers = _signed(self.key, "k1", "/api/data", body, nonce="nonce-tamper-e2e")
        status, data = _send(
            self.port, "POST", "/api/data", headers, b'{"amount":999}'
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(data)["error"]["code"], "bad_signature")

    def test_005_time_window_in_and_out(self):
        # 端到端受真实时钟与网络耗时影响，精确 ±300 边界由注入时钟的单元测试
        # 覆盖；这里用 ±299 验证窗口内、±302 验证窗口外（留足余量）。
        body = b'{"case":"time"}'
        now = int(time.time())
        expected = {now - 299: 200, now + 299: 200,
                    now - 305: 401, now + 305: 401}
        for ts, want in expected.items():
            headers = _signed(self.key, "k1", "/api/data", body, ts=ts)
            status, data = _send(self.port, "POST", "/api/data", headers, body)
            self.assertEqual(status, want, f"ts={ts} -> {data!r}")

    def test_006_equivalent_paths_accepted(self):
        body = b'{"case":"paths"}'
        # http.client 会在客户端折叠 . 段，这里一律走 raw socket 保留原始 target。
        for target in ("/api/data", "/./api/data", "//api//data",
                       "/api/%64ata", "/api/../api/data",
                       "/api/data?b=2&a=1"):
            headers = _signed(self.key, "k1", target, body)
            status, _ = _send_raw(self.port, "POST", target, headers, body)
            self.assertEqual(status, 200, target)

    def test_007_malicious_targets_rejected_with_400(self):
        body = b'{"case":"evil"}'
        # 攻击者绕过合规客户端：服务端在规范化阶段就必须拒绝这些 raw target
        for target in ("/api%2fdata", "/%2e%2e/etc/passwd", "/api/data?a=1&a=2"):
            if "?" in target:
                raw_path, raw_query = target.split("?", 1)
            else:
                raw_path, raw_query = target, ""
            headers = _attacker_signed(
                self.key, "k1", raw_path, raw_query, body
            )
            status, data = _send_raw(self.port, "POST", target, headers, body)
            self.assertEqual(status, 400, target)
            self.assertEqual(
                json.loads(data)["error"]["code"], "malformed_request", target
            )

    def test_008_unknown_key(self):
        headers = _signed(b"x" * 32, "ghost", "/api/data", b"{}")
        status, data = _send(self.port, "POST", "/api/data", headers, b"{}")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(data)["error"]["code"], "unknown_key")

    def test_009_missing_header(self):
        status, data = _send(self.port, "POST", "/api/data", {}, b"{}")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"]["code"], "malformed_request")

    def test_010_signed_get_on_api_path(self):
        headers = _signed(self.key, "k1", "/api/ping", b"", method="GET")
        status, data = _send(self.port, "GET", "/api/ping", headers, b"")
        self.assertEqual(status, 200)

    def test_011_invalid_json_does_not_burn_nonce_and_is_retryable(self):
        # 审查发现 #1：nonce 必须在“生效”前最后一步登记。
        # 已签名但正文非法（400 invalid_json）不应烧掉 nonce——
        # 客户端改正正文后用同一时间戳/nonce/签名的思路不适用（正文变则签名变），
        # 所以这里验证：同样的 nonce 在“认证通过但处理失败”后仍可被一个
        # 正文合法的请求使用（即 nonce 未被登记）。
        good_body = b'{"ok":true}'
        bad_body = b"not-json"

        # 先对 bad_body 签名（认证通过，处理阶段 400）
        bad_headers = _signed(
            self.key, "k1", "/api/data", bad_body, nonce="nonce-retry-001"
        )
        status, data = _send(self.port, "POST", "/api/data", bad_headers, bad_body)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"]["code"], "invalid_json")

        # 同一 nonce 配合法正文：因为 nonce 尚未登记，这条应正常被接受。
        # （需要对新正文重新签名——签名覆盖正文——但 nonce 字符串复用。）
        good_headers = _signed(
            self.key, "k1", "/api/data", good_body, nonce="nonce-retry-001"
        )
        status, _ = _send(self.port, "POST", "/api/data", good_headers, good_body)
        self.assertEqual(status, 200)

        # 此后该 nonce 真正被占用，再用即 409
        status2, _ = _send(self.port, "POST", "/api/data", good_headers, good_body)
        self.assertEqual(status2, 409)

    def test_012_unknown_route_404(self):
        status, _ = _send(self.port, "GET", "/nope", {})
        self.assertEqual(status, 404)

    def test_015_non_ascii_nonce_header_returns_400_not_500(self):
        # 审查发现 #2：审计对原始 nonce 做 ascii 编码会抛异常，把 400 变 500。
        body = b'{"x":1}'
        import socket

        lines = [
            "POST /api/data HTTP/1.1", "Host: 127.0.0.1",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "X-Auth-Key-Id: k1",
            "X-Auth-Timestamp: " + str(int(time.time())),
            "X-Auth-Nonce: nonce-\xe9-abcdefg",  # 非 ASCII 字节 0xE9
            "X-Auth-Signature: " + ("a" * 64),
            "Connection: close",
        ]
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(raw)
            response = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                response += chunk
        status = int(response.split(b"\r\n", 1)[0].split(b" ")[1])
        self.assertEqual(status, 400)
        self.assertIn(b"malformed_request", response)

    def test_013_duplicate_content_length_rejected(self):
        # 请求走私防护：重复的 Content-Length 必须被拒绝，不能挑一个相信。
        body = b'{"case":"smuggle"}'
        headers = _signed(self.key, "k1", "/api/data", body)
        lines = ["POST /api/data HTTP/1.1", "Host: 127.0.0.1"]
        for name, value in headers.items():
            if name == "Content-Length":
                continue
            lines.append(f"{name}: {value}")
        lines.append("Content-Length: " + str(len(body)))
        lines.append("Content-Length: " + str(len(body)))
        lines.append("Connection: close")
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body
        import socket
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(raw)
            response = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                response += chunk
        status = int(response.split(b"\r\n", 1)[0].split(b" ")[1])
        # BaseHTTPRequestHandler 对重复/冲突的 Content-Length 直接 400
        self.assertEqual(status, 400)

    def test_014_oversized_body_rejected_413(self):
        big = b"x" * (1024 * 1024 + 1)
        # 正文本身可以不带合法签名：大小检查在读出正文后、验签路径中先触发。
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(big)),
            "X-Auth-Key-Id": "k1",
            "X-Auth-Timestamp": str(int(time.time())),
            "X-Auth-Nonce": "nonce-oversize-01",
            "X-Auth-Signature": "a" * 64,
        }
        status, data = _send(self.port, "POST", "/api/data", headers, big)
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(data)["error"]["code"], "payload_too_large")


class SqliteEndToEndTests(unittest.TestCase):
    """同一套并发验收在 SQLite nonce 存储上再跑一次。"""

    @classmethod
    def setUpClass(cls):
        cls.fx = ServerFixture(store="sqlite")
        cls.port = cls.fx.port
        cls.key = cls.fx.secret

    @classmethod
    def tearDownClass(cls):
        cls.fx.stop()

    def test_concurrent_duplicates_only_one_200_sqlite(self):
        body = b'{"case":"concurrent-sqlite"}'
        headers = _signed(self.key, "k1", "/api/data", body, nonce="nonce-conc-sql-1")
        n = 32
        barrier = threading.Barrier(n)
        statuses = []

        def one():
            barrier.wait()
            statuses.append(_send(self.port, "POST", "/api/data", dict(headers), body)[0])

        with ThreadPoolExecutor(max_workers=n) as pool:
            list(pool.map(lambda _: one(), range(n)))
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(409), n - 1)


class LogRedactionTests(unittest.TestCase):
    def test_redacting_filter_replaces_known_secret(self):
        secret_hex = "a" * 64
        flt = RedactingFilter([secret_hex])
        record = logging.LogRecord(
            name="anti_replay", level=logging.INFO, pathname=__file__,
            lineno=1, msg="debug dump key=%s", args=(secret_hex,),
            exc_info=None,
        )
        self.assertTrue(flt.filter(record))
        self.assertNotIn(secret_hex, record.getMessage())
        self.assertIn("***REDACTED***", record.getMessage())

    def test_configure_logging_attaches_filter_and_redacts(self):
        stream = io.StringIO()
        logger = configure_logging("INFO", secrets=["deadbeef" * 8])
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        try:
            logger.info("leak attempt: %s", "deadbeef" * 8)
            logger.info("normal event key_id=%s", "demo-key-1")
        finally:
            logger.removeHandler(handler)
        output = stream.getvalue()
        self.assertNotIn("deadbeef", output.replace("***REDACTED***", ""))
        self.assertIn("demo-key-1", output)

    def test_audit_log_contains_no_signature_or_secret(self):
        fx = ServerFixture()
        try:
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            server_logger = fx.server.log
            server_logger.addHandler(handler)

            body = b'{"case":"logcheck"}'
            headers = _signed(fx.secret, "k1", "/api/data", body,
                              nonce="nonce-log-check-1")
            _send(fx.port, "POST", "/api/data", headers, body)
            _send(fx.port, "POST", "/api/data", headers, body)  # 重放
            output = stream.getvalue()

            self.assertNotIn(fx.secret.hex(), output)
            self.assertNotIn(headers["X-Auth-Signature"], output)
            self.assertNotIn(headers["X-Auth-Nonce"],
                             output.replace("nonce_fp=", "X"))
            self.assertIn("request_accepted", output)
            self.assertIn("replay_detected", output)
        finally:
            fx.stop()


if __name__ == "__main__":
    unittest.main()
