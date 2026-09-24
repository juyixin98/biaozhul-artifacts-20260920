"""验收测试：删改、重排、截尾、伪造检查点，以及截尾的可判定/不可判定区分。"""

import base64
import copy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.canonical import canonical_json
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        data_dir=str(tmp_path / "data"), key_dir=str(tmp_path / "keys")
    )
    with TestClient(app) as c:
        yield c


def append_n(client, n):
    for i in range(1, n + 1):
        r = client.post(
            "/entries",
            json={"actor": f"user{i}", "action": "test.op", "payload": {"i": i}},
        )
        assert r.status_code == 201, r.text


def make_anchor(client):
    """创建检查点并作为验证者的可信锚返回 (checkpoint, signature)。"""
    r = client.post("/checkpoints")
    assert r.status_code == 201, r.text
    body = r.json()
    return body["checkpoint"], body["signature"]


def export_all(client):
    r = client.get("/export")
    assert r.status_code == 200, r.text
    return r.json()


def public_key(client):
    return client.get("/public_key").json()["public_key_pem"]


def verify(client, entries, checkpoint=None, checkpoint_signature=None,
           trusted_checkpoint=None, trusted_signature=None):
    body = {
        "entries": entries,
        "public_key_pem": public_key(client),
        "checkpoint": checkpoint,
        "checkpoint_signature": checkpoint_signature,
        "trusted_checkpoint": trusted_checkpoint,
        "trusted_signature": trusted_signature,
    }
    r = client.post("/verify", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 正常路径 ----------

def test_happy_path_valid(client):
    append_n(client, 5)
    cp, sig = make_anchor(client)
    bundle = export_all(client)
    res = verify(
        client,
        bundle["entries"],
        checkpoint=bundle["checkpoint"],
        checkpoint_signature=bundle["checkpoint_signature"],
        trusted_checkpoint=cp,
        trusted_signature=sig,
    )
    assert res["verdict"] == "VALID"
    assert res["chain_valid"] is True
    assert res["verified_up_to_seq"] == 5


def test_checkpoint_signature_is_real(client):
    append_n(client, 3)
    cp, sig = make_anchor(client)
    assert cp["upto_seq"] == 3
    assert cp["head_hash"].startswith("sha256:")
    # 签名应能被公钥验证（这里通过 /verify 的正常路径已覆盖，再做一次直接断言）
    res = verify(client, export_all(client)["entries"],
                 trusted_checkpoint=cp, trusted_signature=sig)
    assert res["verdict"] == "VALID"


# ---------- 删改 ----------

def test_modified_entry_detected(client):
    append_n(client, 4)
    make_anchor(client)
    bundle = export_all(client)
    tampered = copy.deepcopy(bundle["entries"])
    tampered[1]["entry"]["payload"] = {"i": 999}  # 篡改第 2 条内容
    res = verify(client, tampered,
                 checkpoint=bundle["checkpoint"],
                 checkpoint_signature=bundle["checkpoint_signature"])
    assert res["verdict"] == "TAMPERED"
    assert res["chain_error"]["position"] == 1


def test_modified_entry_hash_claim_detected(client):
    """攻击者改内容后把 entry_hash 也改掉，但 prev_hash 链接会断。"""
    append_n(client, 4)
    make_anchor(client)
    bundle = export_all(client)
    tampered = copy.deepcopy(bundle["entries"])
    from app.canonical import hash_object
    tampered[2]["entry"]["actor"] = "mallory"
    tampered[2]["entry_hash"] = hash_object(tampered[2]["entry"])  # 重算本条哈希
    res = verify(client, tampered)
    assert res["verdict"] == "TAMPERED"
    assert res["chain_error"]["position"] == 3  # 下一条的 prev_hash 对不上


# ---------- 删除 ----------

def test_deleted_middle_entry_detected(client):
    append_n(client, 5)
    make_anchor(client)
    bundle = export_all(client)
    tampered = bundle["entries"][:2] + bundle["entries"][3:]  # 删掉第 3 条
    res = verify(client, tampered)
    assert res["verdict"] == "TAMPERED"


# ---------- 重排 ----------

def test_reordered_entries_detected(client):
    append_n(client, 5)
    make_anchor(client)
    bundle = export_all(client)
    tampered = copy.deepcopy(bundle["entries"])
    tampered[1], tampered[3] = tampered[3], tampered[1]  # 交换第 2、4 条
    res = verify(client, tampered)
    assert res["verdict"] == "TAMPERED"


# ---------- 截尾：可判定 vs 不可判定 ----------

def test_truncation_before_anchor_detected(client):
    """截掉锚覆盖范围内的尾部 —— 可信锚可以判定。"""
    append_n(client, 6)
    cp, sig = make_anchor(client)          # 锚覆盖到 seq=6
    bundle = export_all(client)
    truncated = bundle["entries"][:4]      # 攻击者只交出前 4 条
    res = verify(client, truncated,
                 trusted_checkpoint=cp, trusted_signature=sig)
    assert res["verdict"] == "TRUNCATED"
    assert res["verified_up_to_seq"] == 4


def test_truncation_detected_via_log_checkpoint_only(client):
    """没有可信锚时，日志自带的有效签名检查点也能判定截尾。"""
    append_n(client, 6)
    make_anchor(client)                    # 日志方检查点覆盖到 seq=6
    bundle = export_all(client)
    truncated = bundle["entries"][:4]
    res = verify(client, truncated,
                 checkpoint=bundle["checkpoint"],
                 checkpoint_signature=bundle["checkpoint_signature"])
    assert res["verdict"] == "TRUNCATED"


def test_tail_beyond_anchor_unverifiable(client):
    """链尾超出最新可信锚：前缀可信，尾部不可判定。"""
    append_n(client, 4)
    cp, sig = make_anchor(client)          # 锚只覆盖到 seq=4
    append_n(client, 2)                    # 之后又追加 2 条（共 6 条）
    bundle = export_all(client)
    res = verify(client, bundle["entries"],
                 trusted_checkpoint=cp, trusted_signature=sig)
    assert res["verdict"] == "VALID_UP_TO_ANCHOR"
    assert res["verified_up_to_seq"] == 4
    assert "不可判定" in res["detail"]


def test_truncation_after_anchor_undecidable(client):
    """锚之后追加的记录再被删掉：链头恰好等于锚，验证者无法发现。
    服务必须如实报告验证上限，而不是声称整体绝对可信。"""
    append_n(client, 4)
    cp, sig = make_anchor(client)          # 锚覆盖到 seq=4
    append_n(client, 3)                    # 又追加 3 条（共 7 条）
    bundle = export_all(client)
    truncated = bundle["entries"][:4]      # 攻击者删掉锚之后的 3 条
    res = verify(client, truncated,
                 trusted_checkpoint=cp, trusted_signature=sig)
    # 与锚一致，形式上 VALID —— 但 detail 必须说明锚后截尾不可判定
    assert res["verdict"] == "VALID"
    assert res["verified_up_to_seq"] == 4
    assert "不可判定" in res["detail"]


# ---------- 伪造检查点 ----------

def test_forged_checkpoint_wrong_key(client):
    """攻击者用自己的密钥伪造检查点。"""
    append_n(client, 4)
    bundle = export_all(client)
    evil_key = Ed25519PrivateKey.generate()
    forged_cp = {
        "log_id": "audit-log",
        "upto_seq": 4,
        "head_hash": bundle["entries"][-1]["entry_hash"],
        "key_id": "attacker",
        "created_at": "2026-09-24T00:00:00Z",
    }
    forged_sig = base64.urlsafe_b64encode(
        evil_key.sign(canonical_json(forged_cp))
    ).decode().rstrip("=")
    res = verify(client, bundle["entries"],
                 checkpoint=forged_cp, checkpoint_signature=forged_sig)
    assert res["verdict"] == "FORGED_CHECKPOINT"


def test_forged_checkpoint_tampered_fields(client):
    """攻击者改动真实检查点的字段（签名不变）。"""
    append_n(client, 4)
    cp, sig = make_anchor(client)
    bundle = export_all(client)
    forged_cp = dict(cp, upto_seq=2)  # 试图把锚改小，掩盖后续记录
    res = verify(client, bundle["entries"],
                 checkpoint=forged_cp, checkpoint_signature=sig)
    assert res["verdict"] == "FORGED_CHECKPOINT"


def test_forged_trusted_anchor_rejected(client):
    """验证者手里的'锚'本身签名无效时也必须拒绝。"""
    append_n(client, 4)
    bundle = export_all(client)
    bad_anchor = {
        "log_id": "audit-log",
        "upto_seq": 4,
        "head_hash": bundle["entries"][-1]["entry_hash"],
        "key_id": "forged",
        "created_at": "2026-09-24T00:00:00Z",
    }
    res = verify(client, bundle["entries"],
                 trusted_checkpoint=bad_anchor, trusted_signature="AAAA")
    assert res["verdict"] == "FORGED_CHECKPOINT"


# ---------- 无锚 ----------

def test_no_anchor_reports_undecidable(client):
    append_n(client, 3)
    bundle = export_all(client)
    res = verify(client, bundle["entries"])
    assert res["verdict"] == "CHAIN_VALID_NO_ANCHOR"
    assert "不可判定" in res["detail"]


# ---------- 区间导出 ----------

def test_export_range(client):
    append_n(client, 10)
    r = client.get("/export", params={"from_seq": 3, "to_seq": 6})
    assert r.status_code == 200
    body = r.json()
    seqs = [e["entry"]["seq"] for e in body["entries"]]
    assert seqs == [3, 4, 5, 6]
    assert body["checkpoint"] is None  # 尚未创建检查点
