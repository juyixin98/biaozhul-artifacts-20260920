"""验收测试 3：相机 + 分辨率版本绑定、不可变、新旧版本并行查询。"""
from __future__ import annotations

from conftest import (HEIGHT, WIDTH, post_calibration, render_set)
from app import synth


def _calibrate_resolution(client, files, camera, width, height, fov=55.0, seed=7):
    return post_calibration(client, files, camera_id=camera)


def test_new_and_old_versions_coexist_and_queryable(client, good_set):
    """同一相机连续标定两次：两版本都可按 ID 查询，latest 指向新版本。"""
    files, _ = good_set
    r1 = post_calibration(client, files, camera_id="camA")
    assert r1.status_code == 201
    v1 = r1.json()["version_id"]

    r2 = post_calibration(client, files, camera_id="camA")
    v2 = r2.json()["version_id"]
    assert v1 != v2

    # 两版本并行存在，按 ID 精确查询互不影响
    got1 = client.get(f"/api/v1/versions/{v1}").json()
    got2 = client.get(f"/api/v1/versions/{v2}").json()
    assert got1["version_id"] == v1 and got2["version_id"] == v2

    listing = client.get("/api/v1/cameras/camA/versions").json()
    ids = [v["version_id"] for v in listing["versions"]]
    assert ids == [v1, v2]  # 按创建时间排序，旧->新

    latest = client.get(f"/api/v1/cameras/camA/latest",
                        params={"width": WIDTH, "height": HEIGHT}).json()
    assert latest["version_id"] == v2

    # 旧版本仍可取内参（不可变、未被覆盖）
    old_intr = client.get(f"/api/v1/versions/{v1}/intrinsics",
                          params={"width": WIDTH, "height": HEIGHT}).json()
    assert old_intr["intrinsics"]["fx"] > 0


def test_version_not_found(client):
    resp = client.get("/api/v1/versions/nonexistent123")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VERSION_NOT_FOUND"


def test_intrinsics_reject_cross_resolution_reuse(client, good_set, camera_truth):
    """内参严格绑定分辨率：1280x960 的版本不能用于 1024x768 请求（409）。"""
    files, _ = good_set
    rec = post_calibration(client, files, camera_id="camB").json()
    vid = rec["version_id"]

    ok = client.get(f"/api/v1/versions/{vid}/intrinsics",
                    params={"width": WIDTH, "height": HEIGHT})
    assert ok.status_code == 200

    bad = client.get(f"/api/v1/versions/{vid}/intrinsics",
                     params={"width": 1024, "height": 768})
    assert bad.status_code == 409
    detail = bad.json()["detail"]
    assert detail["code"] == "RESOLUTION_MISMATCH"
    assert detail["bound_resolution"] == [WIDTH, HEIGHT]


def test_versions_isolated_by_camera_and_resolution(client, camera_truth):
    """同相机不同分辨率各自维护版本链，互不串用。"""
    K1, dist = camera_truth
    poses = synth.diverse_poses(9, 6, 25.0, distance_mm=520.0, count=18)
    files_hd = render_set(K1, dist, poses, noise=1.0)
    r_hd = post_calibration(client, files_hd, camera_id="camC")
    assert r_hd.status_code == 201, r_hd.text

    K2 = synth.make_camera_matrix(1024, 768, fov_deg=50.0)
    files_sd = render_set(K2, dist, poses, noise=1.0, width=1024, height=768)
    r_sd = post_calibration(client, files_sd, camera_id="camC")
    assert r_sd.status_code == 201, r_sd.text

    listing_all = client.get("/api/v1/cameras/camC/versions").json()
    assert len(listing_all["versions"]) == 2

    only_hd = client.get("/api/v1/cameras/camC/versions",
                         params={"width": WIDTH, "height": HEIGHT}).json()
    assert [v["resolution"] for v in only_hd["versions"]] == [[WIDTH, HEIGHT]]

    # 该相机在 640x480 从未标定 -> latest 明确报错而非返回其他尺寸结果
    missing = client.get("/api/v1/cameras/camC/latest",
                         params={"width": 640, "height": 480})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "VERSION_NOT_FOUND"


def test_version_records_are_immutable(client, storage_dir, good_set):
    """版本文件重复保存被拒绝，且记录里有签名与状态。"""
    files, _ = good_set
    rec = post_calibration(client, files, camera_id="camD").json()
    from app.storage import VersionStore
    store = VersionStore(str(storage_dir))
    import pytest
    with pytest.raises(Exception):
        store.save_version(rec)  # 已存在版本 ID 不可二次写入
    assert rec["state"] == "active" and len(rec["signature"]) == 64
