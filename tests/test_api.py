"""HTTP API 集成测试（FastAPI TestClient，进程内 ASGI）。

覆盖经真实 JSON 序列化路径的判定：向量经 list[float] 编解码、失败类别经
响应 ``code`` 字段返回、request_id 透传。
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from gradbucket.graph import LinearModel, ModelConfig, make_dataset, shard_indices
from gradbucket.server import create_app


PARAMS = [
    {"name": "w", "shape": [2, 3], "dtype": "float64"},
    {"name": "b", "shape": [2], "dtype": "float64"},
    {"name": "spare", "shape": [2], "dtype": "float64"},
]
ZERO_WEIGHTS = {"w": np.zeros((2, 3)).tolist(), "b": np.zeros(2).tolist(),
                "spare": np.zeros(2).tolist()}


@pytest.fixture
def client():
    app = create_app(liveness_timeout=0.4)
    with TestClient(app) as c:
        yield c


def begin(client, sizes=None):
    resp = client.post("/rounds/begin", json={
        "round_index": 0,
        "params": PARAMS,
        "weights": ZERO_WEIGHTS,
        "workers": ["w0", "w1", "w2"],
        "bucket_capacity": 2,
        "known_zero_params": ["spare"],
    })
    assert resp.status_code == 200
    return resp.json()


def shard_payloads(sizes):
    x, y = make_dataset(12, seed=445, n_in=3, n_out=2)
    out = {}
    for rank, w in enumerate(["w0", "w1", "w2"]):
        idx = shard_indices(12, rank, 3, shard_sizes=sizes, seed=445)
        grads, n = LinearModel(ModelConfig()).gradients(x[idx], y[idx])
        # 桶0 = w(6)+b(2)，桶1 = spare(2)
        vec0 = np.concatenate([grads["w"].reshape(-1), grads["b"]]).tolist()
        out[w] = {
            0: (vec0, [True] * 8, n),
            1: (grads["spare"].tolist(), [True, True], n),
        }
    return out


def submit(client, w, b, vec, mask, n, generation=0, rid=None):
    body = {"generation": generation, "worker_id": w, "vec": vec,
            "mask": mask, "sample_count": n}
    if rid:
        body["request_id"] = rid
    return client.post(f"/rounds/0/buckets/{b}", json=body)


def test_health_and_begin_layout_is_fixed(client):
    assert client.get("/health").json()["status"] == "ok"
    view = begin(client)["round_view"]
    assert view["bucket_sizes"] == [2, 1]
    assert [s["param"] for s in view["slots"]] == ["w", "b", "spare"]
    assert view["status"] == "OPEN"


def test_full_round_over_http_accepted_with_basis_and_generation(client):
    begin(client)
    payloads = shard_payloads([2, 3, 7])
    for w in ["w0", "w1", "w2"]:
        client.post(f"/workers/{w}/heartbeat")
        for b in (0, 1):
            vec, mask, n = payloads[w][b]
            resp = submit(client, w, b, vec, mask, n)
            assert resp.json()["verdict"] == "ACCEPTED"

    # 封存前权重始终为轮初零值，世代为 0。
    wmsg = client.get("/weights").json()
    assert wmsg["generation"] == 0
    assert np.all(np.asarray(wmsg["weights"]["w"]) == 0.0)

    seal = client.post("/rounds/0/seal").json()
    assert seal["verdict"] == "ACCEPTED"
    commit = seal["commit"]
    assert (commit["generation_before"], commit["generation_after"]) == (0, 1)
    # 归约依据：w 槽分母 12，参与样本数 2/3/7。
    slot_w = commit["bucket_bases"][0]["slots"][0]
    assert slot_w["sample_counts"] == [2, 3, 7]
    assert slot_w["weight_total"] == pytest.approx(12.0)
    assert slot_w["covered"] is True
    # 权重数值与 -0.1 * 联合批梯度一致（独立参照路径）。
    from gradbucket.reference import union_batch_gradient
    x, y = make_dataset(12, seed=445, n_in=3, n_out=2)
    shards = [(x[shard_indices(12, r, 3, shard_sizes=[2, 3, 7], seed=445)],
               y[shard_indices(12, r, 3, shard_sizes=[2, 3, 7], seed=445)])
              for r in range(3)]
    ref, n_total = union_batch_gradient(LinearModel(ModelConfig()), shards)
    assert n_total == 12
    np.testing.assert_allclose(commit["weights"]["w"], -0.1 * ref["w"], atol=1e-12)
    np.testing.assert_allclose(commit["weights"]["b"], -0.1 * ref["b"], atol=1e-12)


def test_weights_visible_to_late_worker_are_pre_commit_snapshot(client):
    begin(client)
    payloads = shard_payloads(None)
    for w in ["w0", "w1"]:  # 先交两个人的桶 0，桶 0 尚未齐
        client.post(f"/workers/{w}/heartbeat")
        vec, mask, n = payloads[w][0]
        submit(client, w, 0, vec, mask, n)
    assert client.get("/weights").json()["generation"] == 0
    # 第三个人交齐桶0（STAGED），但世代仍不变。
    client.post("/workers/w2/heartbeat")
    vec, mask, n = payloads["w2"][0]
    submit(client, "w2", 0, vec, mask, n)
    assert client.get("/weights").json()["generation"] == 0
    view = client.get("/rounds/current").json()
    assert view["staged_buckets"] == [0]


def test_stale_generation_rejected_with_stable_code(client):
    begin(client)
    client.post("/workers/w0/heartbeat")
    resp = submit(client, "w0", 0, [0.0] * 8, [True] * 8, 4,
                  generation=42, rid="stale-1")
    assert resp.json()["code"] == "REJECTED_STALE_ROUND"
    diags = client.get("/diagnostics").json()
    rec = [r for r in diags["records"] if r["request_id"] == "stale-1"][0]
    assert rec["verdict"] == "REJECTED_STALE_ROUND"
    assert rec["worker_id"] == "w0"


def test_duplicate_and_bad_shape_categories(client):
    begin(client)
    client.post("/workers/w0/heartbeat")
    r1 = submit(client, "w0", 0, [0.0] * 8, [True] * 8, 4)
    assert r1.json()["code"] == "ACCEPTED"
    r2 = submit(client, "w0", 0, [0.0] * 8, [True] * 8, 4)
    assert r2.json()["code"] == "REJECTED_DUPLICATE"
    r3 = submit(client, "w1", 0, [0.0] * 7, [True] * 7, 4)
    assert r3.json()["code"] == "REJECTED_BUCKET_SHAPE"


def test_unknown_worker_heartbeat_404(client):
    begin(client)
    resp = client.post("/workers/ghost/heartbeat")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "UNKNOWN_WORKER"


@pytest.mark.integration
def test_worker_loss_detected_over_real_timeout_round_rejected(client):
    begin(client)
    payloads = shard_payloads(None)
    # w2 只交桶 0，之后彻底失联；w0,w1 正常交完。
    for w in ["w0", "w1"]:
        client.post(f"/workers/{w}/heartbeat")
        for b in (0, 1):
            vec, mask, n = payloads[w][b]
            assert submit(client, w, b, vec, mask, n).json()["code"] == "ACCEPTED"
    vec, mask, n = payloads["w2"][0]
    assert submit(client, "w2", 0, vec, mask, n).json()["code"] == "ACCEPTED"

    # 超过失联阈值期间只给存活者续约。
    deadline = time.time() + 0.6
    while time.time() < deadline:
        client.post("/workers/w0/heartbeat")
        client.post("/workers/w1/heartbeat")
        time.sleep(0.05)

    seal = client.post("/rounds/0/seal").json()
    assert seal["code"] == "REJECTED_WORKER_LOST"
    assert "commit" not in seal
    assert seal["round_view"]["status"] == "REJECTED"
    # 被拒轮不接受迟到提交。
    late = submit(client, "w2", 1, [0.0, 0.0], [True, True], 4)
    assert late.json()["code"] == "REJECTED_SEALED"


def test_missing_bucket_within_timeout_is_indeterminate(client):
    begin(client)
    payloads = shard_payloads(None)
    for w in ["w0", "w1", "w2"]:
        client.post(f"/workers/{w}/heartbeat")
        vec, mask, n = payloads[w][0]
        submit(client, w, 0, vec, mask, n)
    seal = client.post("/rounds/0/seal").json()
    assert seal["code"] == "INDETERMINATE_PENDING"
    assert seal["round_view"]["status"] == "OPEN"


def test_begin_conflicts_and_bad_spec_return_stable_codes_not_500(client):
    begin(client)
    # 已有开放轮再 begin -> 409。
    resp = client.post("/rounds/begin", json={
        "round_index": 1, "params": PARAMS, "weights": ZERO_WEIGHTS,
        "workers": ["w0", "w1", "w2"], "bucket_capacity": 2,
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "ROUND_ALREADY_OPEN"
    # 重复工作者 -> 422。
    client.post("/rounds/reset")  # 当前轮 OPEN，先验证 reset 会被 409 拒绝
    resp = client.post("/rounds/reset")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "ROUND_STILL_OPEN"


def test_begin_with_duplicate_workers_is_422(client):
    resp = client.post("/rounds/begin", json={
        "round_index": 0, "params": PARAMS, "weights": ZERO_WEIGHTS,
        "workers": ["w0", "w0"], "bucket_capacity": 2,
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_ROUND_SPEC"


def test_seal_is_idempotent_over_http(client):
    begin(client)
    payloads = shard_payloads(None)
    for w in ["w0", "w1", "w2"]:
        client.post(f"/workers/{w}/heartbeat")
        for b in (0, 1):
            vec, mask, n = payloads[w][b]
            submit(client, w, b, vec, mask, n)
    first = client.post("/rounds/0/seal")
    second = client.post("/rounds/0/seal")  # 网络重试
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["code"] == "ACCEPTED"
    assert second.json()["code"] == "ACCEPTED"
    assert (second.json()["commit"]["generation_after"]
            == first.json()["commit"]["generation_after"] == 1)


def test_seal_missing_round_is_404_not_500(client):
    resp = client.post("/rounds/99/seal")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "ROUND_NOT_FOUND"


def test_heartbeat_response_id_is_the_one_coordinator_uses(client):
    begin(client)
    resp = client.post("/workers/w0/heartbeat")
    rid = resp.json()["request_id"]
    assert rid.startswith("hb_")
    # 未知工作者：响应 404，且诊断记录使用传入/生成的 request_id 可关联。
    app_coord = client.app.state.coordinator
    import pytest as _pytest
    with _pytest.raises(KeyError):
        app_coord.heartbeat("ghost", request_id="hb-ghost-1")
    rec = [d for d in app_coord.diagnostics().all()
           if d.request_id == "hb-ghost-1"]
    assert len(rec) == 1 and rec[0].worker_id == "ghost"
    # 成功心跳返回的正是协调器内部实际使用的 id（非端点另造）。
    used = app_coord.heartbeat("w0", request_id="hb-real-1")
    assert used == "hb-real-1"


def test_bool_sample_count_rejected_at_http_boundary(client):
    begin(client)
    client.post("/workers/w0/heartbeat")
    resp = client.post("/rounds/0/buckets/0", json={
        "generation": 0, "worker_id": "w0",
        "vec": [0.0] * 8, "mask": [True] * 8, "sample_count": True,
    })
    # pydantic StrictInt：布尔不是合法样本数，422 而非被当作 1 接受。
    assert resp.status_code == 422
