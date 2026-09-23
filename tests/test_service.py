"""本地 HTTP 服务端到端测试（真实 socket + urllib，无外部依赖）。"""

import base64
import contextlib
import hashlib
import json
import threading
import unittest
import urllib.request
import urllib.error

from tss.service import build_server, ServerConfig


@contextlib.contextmanager
def running_server():
    server = build_server(ServerConfig(host="127.0.0.1", port=0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post(server, path, payload):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def get(server, path):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class TestHTTPService(unittest.TestCase):
    def test_healthz(self):
        with running_server() as s:
            status, body = get(s, "/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "ok")

    def test_404(self):
        with running_server() as s:
            status, _ = post(s, "/nope", {})
            self.assertEqual(status, 404)

    def test_split_and_recover_text(self):
        with running_server() as s:
            status, body = post(s, "/split", {
                "secret_text": "服务端恢复测试", "threshold": 3, "total": 5,
            })
            self.assertEqual(status, 200)
            self.assertEqual(len(body["shares"]), 5)
            split_id = body["split_id"]

            status, rec = post(s, "/recover", {"shares": body["shares"][0:3]})
            self.assertEqual(status, 200)
            self.assertEqual(rec["secret_text"], "服务端恢复测试")
            self.assertEqual(rec["split_id"], split_id)
            self.assertEqual(rec["used_share_count"], 3)

    def test_split_and_recover_b64(self):
        secret = bytes(range(256))
        with running_server() as s:
            status, body = post(s, "/split", {
                "secret_b64": base64.standard_b64encode(secret).decode(),
                "threshold": 2, "total": 3,
            })
            self.assertEqual(status, 200)
            status, rec = post(s, "/recover", {"shares": body["shares"]})
            self.assertEqual(status, 200)
            self.assertEqual(base64.standard_b64decode(rec["secret_b64"]), secret)

    def test_recover_below_threshold(self):
        with running_server() as s:
            _, body = post(s, "/split", {
                "secret_text": "x", "threshold": 3, "total": 5,
            })
            status, err = post(s, "/recover", {"shares": body["shares"][:2]})
            self.assertEqual(status, 409)
            self.assertEqual(err["error"], "below_threshold")

    def test_recover_duplicate_x(self):
        with running_server() as s:
            _, body = post(s, "/split", {
                "secret_text": "x", "threshold": 3, "total": 5,
            })
            shares = [body["shares"][0], body["shares"][0], body["shares"][1]]
            status, err = post(s, "/recover", {"shares": shares})
            self.assertEqual(status, 409)
            self.assertEqual(err["error"], "duplicate_index")

    def test_mixed_batch(self):
        with running_server() as s:
            _, a = post(s, "/split", {"secret_text": "a", "threshold": 2, "total": 3})
            _, b = post(s, "/split", {"secret_text": "b", "threshold": 2, "total": 3})
            status, err = post(s, "/recover", {
                "shares": [a["shares"][0], a["shares"][1], b["shares"][2]],
            })
            self.assertEqual(status, 409)
            self.assertEqual(err["error"], "inconsistent_shares")
            self.assertIn(2, err["suspect"])

    def test_malformed_encoding_isolated(self):
        with running_server() as s:
            _, body = post(s, "/split", {
                "secret_text": "robust", "threshold": 2, "total": 4,
            })
            status, rec = post(s, "/recover", {
                "shares": ["!!!not-base64!!!", body["shares"][1],
                           body["shares"][2], body["shares"][3]],
            })
            self.assertEqual(status, 200)
            self.assertEqual(rec["secret_text"], "robust")
            self.assertEqual(rec["rejected"][0]["reason"], "malformed_share")

    def test_all_bad_shares(self):
        with running_server() as s:
            status, err = post(s, "/recover", {"shares": ["garbage", "???"]})
            self.assertEqual(status, 409)
            self.assertEqual(err["error"], "inconsistent_shares")
            self.assertEqual(len(err["rejected"]), 2)

    def test_corrupted_checksum_share(self):
        with running_server() as s:
            _, body = post(s, "/split", {
                "secret_text": "bits-flip", "threshold": 2, "total": 4,
            })
            raw = bytearray(base64.standard_b64decode(body["shares"][0]))
            raw[30] ^= 0x01
            flipped = base64.standard_b64encode(bytes(raw)).decode()
            # 坏份额被标签拒绝并隔离；3 个好份额 > 阈值 2，诊断一致 -> 恢复成功
            status, rec = post(s, "/recover", {
                "shares": [flipped] + body["shares"][1:4],
            })
            self.assertEqual(status, 200)
            self.assertEqual(rec["secret_text"], "bits-flip")
            self.assertEqual(rec["rejected"][0]["reason"], "integrity_failure")

    def test_corrupted_checksum_below_threshold(self):
        with running_server() as s:
            _, body = post(s, "/split", {
                "secret_text": "bits-flip", "threshold": 2, "total": 3,
            })
            raw = bytearray(base64.standard_b64decode(body["shares"][0]))
            raw[30] ^= 0x01
            flipped = base64.standard_b64encode(bytes(raw)).decode()
            # 1 坏 + 2 好：好份额恰好 t=2 时直接插值恢复（坏份额已隔离）
            status, rec = post(s, "/recover", {
                "shares": [flipped] + body["shares"][1:3],
            })
            self.assertEqual(status, 200)
            self.assertEqual(rec["secret_text"], "bits-flip")
            self.assertEqual(rec["used_share_count"], 2)
            self.assertEqual(len(rec["rejected"]), 1)

    def test_authenticated_mode(self):
        key = base64.standard_b64encode(b"0" * 32).decode()
        with running_server() as s:
            status, body = post(s, "/split", {
                "secret_text": "authenticated", "threshold": 3, "total": 5,
                "auth_key_b64": key,
            })
            self.assertEqual(status, 200)
            self.assertTrue(body["authenticated"])

            status, rec = post(s, "/recover", {
                "shares": body["shares"][:3], "auth_key_b64": key,
            })
            self.assertEqual(status, 200)
            self.assertEqual(rec["secret_text"], "authenticated")

            # 不带密钥：fail-closed
            status, err = post(s, "/recover", {"shares": body["shares"][:3]})
            self.assertEqual(status, 409)

            # 错误密钥：HMAC 失败 -> 全部不可用
            wrong = base64.standard_b64encode(b"1" * 32).decode()
            status, err = post(s, "/recover", {
                "shares": body["shares"][:3], "auth_key_b64": wrong,
            })
            self.assertEqual(status, 409)

    def test_forged_share_with_recomputed_checksum_detected_by_diagnosis(self):
        # n=t+1=4, t=3：检测到伪造即拒绝（不保证定位具体下标）
        with running_server() as s:
            _, body = post(s, "/split", {
                "secret_text": "forged", "threshold": 3, "total": 4,
            })
            raw = bytearray(base64.standard_b64decode(body["shares"][3]))
            raw[30] ^= 0xAA
            raw[-32:] = hashlib.sha256(bytes(raw[:-32])).digest()
            forged = base64.standard_b64encode(bytes(raw)).decode()
            status, err = post(s, "/recover", {
                "shares": body["shares"][:3] + [forged],
            })
            self.assertEqual(status, 409)
            self.assertEqual(err["error"], "inconsistent_shares")
            self.assertIn("votes", err)

    def test_forged_share_locatable_with_n_ge_2t_minus_1(self):
        # n=5, t=3，秘密较长：冗余足够时可定位伪造份额（启发式，非密码学保证）
        with running_server() as s:
            _, body = post(s, "/split", {
                "secret_text": "forged-need-locating-0123456789",
                "threshold": 3, "total": 5,
            })
            raw = bytearray(base64.standard_b64decode(body["shares"][4]))
            raw[30] ^= 0xAA
            raw[-32:] = hashlib.sha256(bytes(raw[:-32])).digest()
            forged = base64.standard_b64encode(bytes(raw)).decode()
            status, err = post(s, "/recover", {
                "shares": body["shares"][:4] + [forged],
            })
            self.assertEqual(status, 409)
            self.assertIn(4, err["suspect"])

    def test_validate_endpoint(self):
        with running_server() as s:
            _, body = post(s, "/split", {
                "secret_text": "peek", "threshold": 2, "total": 3,
            })
            status, info = post(s, "/validate", {"share": body["shares"][0]})
            self.assertEqual(status, 200)
            self.assertEqual(info["field"], "GF(2^8)-Rijndael-0x11b")
            self.assertEqual(info["x"], 1)

    def test_invalid_json(self):
        with running_server() as s:
            url = f"http://127.0.0.1:{s.server_address[1]}/split"
            req = urllib.request.Request(url, data=b"{not json",
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("expected HTTPError")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)
                body = json.loads(exc.read().decode())
                self.assertEqual(body["error"], "invalid_params")

    def test_bad_params_http(self):
        with running_server() as s:
            status, err = post(s, "/split", {
                "secret_text": "x", "threshold": 1, "total": 3,
            })
            self.assertEqual(status, 400)
            self.assertEqual(err["error"], "invalid_params")

            status, err = post(s, "/split", {"threshold": 2, "total": 3})
            self.assertEqual(status, 400)

    def test_bad_base64_secret(self):
        with running_server() as s:
            status, err = post(s, "/split", {
                "secret_b64": "!!!", "threshold": 2, "total": 3,
            })
            self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
