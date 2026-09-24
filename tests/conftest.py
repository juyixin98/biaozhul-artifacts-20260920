"""pytest 公共夹具：每个测试隔离的存储目录 + 合成投影数据生成器。"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as config_module  # noqa: E402
from app import crypto as crypto_module  # noqa: E402
from app import main as main_module  # noqa: E402
from app import synth  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PATTERN = (9, 6)
SQUARE_MM = 25.0
WIDTH, HEIGHT = 1280, 960


@pytest.fixture
def storage_dir(tmp_path, monkeypatch):
    """把存储目录与 HMAC 密钥隔离到临时路径（settings 为 frozen 单例，直接改属性）。"""
    path = tmp_path / "data"
    monkeypatch.delenv("CALIB_HMAC_SECRET", raising=False)
    object.__setattr__(config_module.settings, "storage_dir", str(path))
    crypto_module._SECRET_CACHE = None
    yield path
    crypto_module._SECRET_CACHE = None


@pytest.fixture
def client(storage_dir):
    importlib.reload(main_module)
    main_module._store = None
    with TestClient(main_module.app) as c:
        yield c


@pytest.fixture
def camera_truth():
    """合成相机真值（渲染端使用，标定端不可见）。"""
    K = synth.make_camera_matrix(WIDTH, HEIGHT, fov_deg=55.0)
    dist = np.array([[-0.12], [0.03], [0.0005], [-0.0005], [-0.005]], np.float64)
    return K, dist


def render_set(K, dist, poses, *, noise=1.0, width=WIDTH, height=HEIGHT,
               pattern=PATTERN, square_mm=SQUARE_MM, ss=2):
    """渲染一组棋盘 PNG 字节，返回 [(name, bytes)]。"""
    out = []
    for i, (rvec, tvec) in enumerate(poses):
        img = synth.render_board(width, height, pattern[0], pattern[1], square_mm,
                                 rvec, tvec, K, dist, ss=ss, noise_sigma=noise)
        out.append((f"view_{i:02d}.png", synth.encode_png(img)))
    return out


@pytest.fixture
def good_set(camera_truth):
    K, dist = camera_truth
    poses = synth.diverse_poses(PATTERN[0], PATTERN[1], SQUARE_MM,
                                distance_mm=520.0, count=18)
    return render_set(K, dist, poses, noise=1.0), K


@pytest.fixture
def bad_images():
    """各类异常图字节：强模糊弱纹理、平滑渐变、截断 PNG、非图像字节。"""
    rng = np.random.default_rng(3)
    h, w = HEIGHT, WIDTH
    noise_small = rng.integers(0, 256, (h // 8, w // 8, 3), dtype=np.uint8)
    noise = cv2.resize(noise_small, (w, h), interpolation=cv2.INTER_LINEAR)
    noise = cv2.GaussianBlur(noise, (101, 101), 40.0)
    grad = np.repeat(
        np.tile(np.linspace(0, 255, w, dtype=np.uint8), (h, 1))[:, :, None], 3, axis=2
    )
    return [
        ("noise.png", synth.encode_png(noise)),
        ("gradient.png", synth.encode_png(grad)),
        ("truncated.png", synth.encode_png(grad)[:120]),
        ("garbage.jpg", b"not an image at all"),
    ]


def post_calibration(client, files, camera_id="cam1", board=PATTERN,
                     square_mm=SQUARE_MM, min_views=None):
    data = {
        "camera_id": camera_id,
        "pattern_cols": str(board[0]),
        "pattern_rows": str(board[1]),
        "square_size_mm": str(square_mm),
    }
    if min_views is not None:
        data["min_views"] = str(min_views)
    return client.post(
        "/api/v1/calibrations",
        data=data,
        files=[("images", (name, raw, "image/png")) for name, raw in files],
    )
