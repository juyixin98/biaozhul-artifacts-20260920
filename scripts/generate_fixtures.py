"""Generate synthetic chessboard images by projecting with a *known* camera.

These fixtures let acceptance tests run the real calibration pipeline and
compare recovered intrinsics against ground truth instead of trusting canned
matrices.

Layout under the output directory:
  ground_truth.json          known K, D, image size, board spec
  valid_views/view_XX.png    ~18 diverse poses around the board
  repeated_views/view_XX.png 18 views, several near-duplicates
  planar_arc/                degenerate: camera translates on a line, board
                             kept fronto-parallel
  corrupted/                 one undecodable file + a no-board photo
  other_resolution/          same board rendered at 1024x768
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROWS, COLS = 6, 9
SQUARE_MM = 40.0
WIDTH, HEIGHT = 1280, 960
ALT_WIDTH, ALT_HEIGHT = 1024, 768


def ground_truth_camera(width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    fx = fy = width * 0.7421875  # 950 at 1280, scales with resolution
    K = np.array(
        [[fx, 0.0, width / 2.0], [0.0, fy, height / 2.0], [0.0, 0.0, 1.0]]
    )
    # realistic moderate distortion (k1, k2, p1, p2, k3=0; the service uses
    # the standard model with k3 fixed, as high-order k3 is unidentifiable
    # outside the extreme image periphery)
    D = np.array([[-0.28, 0.09, 0.0012, -0.0009, 0.0]])
    return K, D


def board_object_points() -> np.ndarray:
    objp = np.zeros((ROWS * COLS, 3), np.float32)
    xs, ys = np.meshgrid(np.arange(COLS), np.arange(ROWS))
    objp[:, :2] = np.stack([xs.ravel(), ys.ravel()], axis=1) * SQUARE_MM
    return objp


def make_pose(
    azimuth_deg: float,
    elevation_deg: float,
    distance_mm: float = 700.0,
    roll_deg: float = 0.0,
    target_offset_mm: tuple[float, float] = (0.0, 0.0),
) -> tuple[np.ndarray, np.ndarray]:
    """Camera pose looking at a target point on the board plane.

    Uses the classic OpenCV board frame: board in z=0, camera on the -z
    side looking down +z, world up is +y. azimuth moves the camera sideways,
    elevation up/down. ``target_offset_mm`` shifts the aim point relative to
    the board centre, which pushes the projected board toward image edges so
    distortion there becomes observable.
    """
    az = np.radians(azimuth_deg)
    el = np.radians(elevation_deg)
    centre = np.array(
        [
            (COLS - 1) * SQUARE_MM / 2 + target_offset_mm[0],
            (ROWS - 1) * SQUARE_MM / 2 + target_offset_mm[1],
            0.0,
        ]
    )
    cam_pos = centre + distance_mm * np.array(
        [np.cos(el) * np.sin(az), np.sin(el), -np.cos(el) * np.cos(az)]
    )
    # camera z axis points from camera toward board (+z world at az=el=0)
    forward = centre - cam_pos
    forward /= np.linalg.norm(forward)
    world_up = np.array([0.0, 1.0, 0.0])
    x_cam = np.cross(world_up, forward)
    x_cam /= np.linalg.norm(x_cam)
    y_cam = np.cross(forward, x_cam)  # rows grow downward
    R = np.vstack([x_cam, y_cam, forward])
    if roll_deg:
        roll = np.radians(roll_deg)
        Rroll = np.array(
            [
                [np.cos(roll), -np.sin(roll), 0.0],
                [np.sin(roll), np.cos(roll), 0.0],
                [0.0, 0.0, 1.0],
            ]
        )
        R = Rroll @ R
    tvec = -R @ cam_pos
    rvec, _ = cv2.Rodrigues(R.astype(np.float64))
    return rvec, tvec.reshape(3, 1).astype(np.float64)


def render_view(
    rvec: np.ndarray,
    tvec: np.ndarray,
    K: np.ndarray,
    D: np.ndarray,
    width: int,
    height: int,
) -> np.ndarray:
    """Draw a perspective-projected, distorted checkerboard image.

    Geometry convention matches the calibration pipeline:
      * board_object_points() gives the COLS x ROWS *inner* corner positions
        (0..COLS-1, 0..ROWS-1) in square units;
      * the physical target is (COLS+1) x (ROWS+1) *squares* big; an outer
        ring of squares beyond the corner grid gives findChessboardCorners
        its quiet border;
      * the cell whose nearest-corner indices are (c, r) is centred on the
        inner corner (c, r), so the cell lattice is shifted half a square
        outward relative to the object-point lattice.
    """
    # Render at 2x supersampling then downscale: real camera anti-aliasing
    # without quantization-limited edge jitter.
    ss = 2
    K_ss = K.copy()
    K_ss[0, 0] *= ss
    K_ss[1, 1] *= ss
    K_ss[0, 2] *= ss
    K_ss[1, 2] *= ss
    big = np.full((height * ss, width * ss, 3), 235, np.uint8)

    half = SQUARE_MM / 2.0
    # White board face under the whole square lattice, projected with the
    # same distortion the calibration has to recover.
    face = np.array(
        [
            [-half, -half, 0.0],
            [COLS * SQUARE_MM + half, -half, 0.0],
            [COLS * SQUARE_MM + half, ROWS * SQUARE_MM + half, 0.0],
            [-half, ROWS * SQUARE_MM + half, 0.0],
        ],
        dtype=np.float64,
    )
    fp, _ = cv2.projectPoints(face, rvec, tvec, K_ss, D)
    cv2.fillConvexPoly(big, np.int32(fp.reshape(-1, 2)), (245, 245, 245), cv2.LINE_AA)

    # Unit square centred on (0,0) in board-plane coordinates.
    unit_square = (
        np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float64) - 0.5
    )
    for r in range(ROWS + 1):
        for c in range(COLS + 1):
            if (r + c) % 2 != 0:
                continue
            centre = np.array([c * SQUARE_MM, r * SQUARE_MM, 0.0], np.float64)
            plane_pts = centre + np.pad(unit_square * SQUARE_MM, ((0, 0), (0, 1)))
            projected, _ = cv2.projectPoints(plane_pts, rvec, tvec, K_ss, D)
            cv2.fillConvexPoly(
                big, np.int32(projected.reshape(-1, 2)), (25, 25, 25), cv2.LINE_AA
            )

    img = cv2.resize(big, (width, height), interpolation=cv2.INTER_AREA)
    return img


def write_view(
    path: Path,
    az: float,
    el: float,
    dist: float,
    roll: float,
    K: np.ndarray,
    D: np.ndarray,
    width: int,
    height: int,
    offset: tuple[float, float] = (0.0, 0.0),
) -> None:
    rvec, tvec = make_pose(az, el, dist, roll, offset)
    img = render_view(rvec, tvec, K, D, width, height)
    cv2.imwrite(str(path), img)


# (filename, azimuth, elevation, distance, roll, aim-offset x/y in mm)
# Deterministic wide-coverage set: central/slanted views plus off-centre aim
# points so the board samples the distorted image periphery.
def diverse_poses() -> list[tuple]:
    d = 1000.0
    ox, oy = 140.0, 100.0
    poses = [
        ("view_00.png",   0,   0, d,  0.0, (0, 0)),
        ("view_01.png",  15,  12, d, -4.0, (0, 0)),
        ("view_02.png", -16,  11, d,  4.0, (0, 0)),
        ("view_03.png",  17, -13, d,  3.0, (0, 0)),
        ("view_04.png", -15, -14, d, -3.0, (0, 0)),
        ("view_05.png",  30,   0, d,  0.0, (0, 0)),
        ("view_06.png", -31,   0, d,  0.0, (0, 0)),
        ("view_07.png",   0,  24, d,  5.0, (0, 0)),
        ("view_08.png",   0, -24, d, -5.0, (0, 0)),
        ("view_09.png",  24,  20, d, -6.0, (0, 0)),
        ("view_10.png", -25, -21, d,  6.0, (0, 0)),
        ("view_11.png",  28, -19, d,  2.0, (0, 0)),
        ("view_12.png", -29,  18, d, -2.0, (0, 0)),
        ("view_13.png",  10,   8, d,  0.0, ( ox, 0)),
        ("view_14.png", -11,   9, d,  0.0, (-ox, 0)),
        ("view_15.png",  10,  -9, d,  0.0, (0,  oy)),
        ("view_16.png", -11,  -8, d,  0.0, (0, -oy)),
        ("view_17.png",  22,  16, d,  3.0, ( ox,  oy)),
        ("view_18.png", -23,  17, d, -3.0, (-ox,  oy)),
        ("view_19.png",  24, -18, d,  3.0, ( ox, -oy)),
        ("view_20.png", -22, -17, d, -3.0, (-ox, -oy)),
        ("view_21.png",  33,  10, d,  4.0, ( ox, 0)),
        ("view_22.png", -34, -11, d, -4.0, (-ox, 0)),
        ("view_23.png",   6,  26, d, -2.0, (0, -oy)),
    ]
    return poses


def main(out_dir: str) -> None:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    K, D = ground_truth_camera(WIDTH, HEIGHT)
    K_alt, D_alt = ground_truth_camera(ALT_WIDTH, ALT_HEIGHT)

    gt = {
        "width": WIDTH,
        "height": HEIGHT,
        "board_rows": ROWS,
        "board_cols": COLS,
        "square_size_mm": SQUARE_MM,
        "camera_matrix": K.tolist(),
        "dist_coeffs": D.reshape(-1).tolist(),
    }
    (root / "ground_truth.json").write_text(json.dumps(gt, indent=2))

    valid = root / "valid_views"
    valid.mkdir(exist_ok=True)
    for name, az, el, dist, roll, offset in diverse_poses():
        write_view(valid / name, az, el, dist, roll, K, D, WIDTH, HEIGHT, offset)

    # repeated views: 18 good distinct poses + 6 near-identical extras
    rep = root / "repeated_views"
    rep.mkdir(exist_ok=True)
    base = diverse_poses()
    for name, az, el, dist, roll, offset in base:
        write_view(rep / name, az, el, dist, roll, K, D, WIDTH, HEIGHT, offset)
    for j in range(6):
        name, az, el, dist, roll, offset = base[j]
        write_view(
            rep / f"dup_{j:02d}.png",
            az + 0.3,
            el + 0.2,
            dist + 2.0,
            roll + 0.2,
            K,
            D,
            WIDTH,
            HEIGHT,
            offset,
        )

    # degenerate: fronto-parallel board, camera only shifts sideways on a line
    deg = root / "planar_arc"
    deg.mkdir(exist_ok=True)
    for i, shift in enumerate(np.linspace(-260, 260, 18)):
        rvec, tvec = make_pose(0, 0, 1000.0, 0.0)
        tvec = tvec + np.array([[shift], [0.0], [0.0]])
        img = render_view(rvec, tvec, K, D, WIDTH, HEIGHT)
        cv2.imwrite(str(deg / f"view_{i:02d}.png"), img)

    # corrupted inputs
    bad = root / "corrupted"
    bad.mkdir(exist_ok=True)
    (bad / "not_an_image.png").write_bytes(b"this is definitely not a PNG" * 20)
    blank = np.full((HEIGHT, WIDTH, 3), 200, np.uint8)
    cv2.putText(
        blank, "no chessboard here", (200, 480), cv2.FONT_HERSHEY_SIMPLEX, 2,
        (40, 40, 40), 4,
    )
    cv2.imwrite(str(bad / "no_board.png"), blank)
    wrong_size = np.full((HEIGHT - 120, WIDTH, 3), 235, np.uint8)
    cv2.imwrite(str(bad / "wrong_resolution.png"), wrong_size)

    # a second resolution for binding tests (fx scales with resolution, so
    # distance scales the same way to keep angular coverage identical)
    alt = root / "other_resolution"
    alt.mkdir(exist_ok=True)
    scale = ALT_WIDTH / WIDTH
    for name, az, el, dist, roll, offset in diverse_poses():
        write_view(
            alt / name, az, el, dist * scale, roll,
            K_alt, D_alt, ALT_WIDTH, ALT_HEIGHT,
            (offset[0] * scale, offset[1] * scale),
        )

    print(f"fixtures written under {root}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "examples/fixtures"
    main(target)
