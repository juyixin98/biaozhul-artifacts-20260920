"""Spatial chunking and confidence-based merge of overlapping classifications.

Points are processed in axis-aligned XY windows with an overlap band, so the
same global point (identified by its index in the original array) can be
classified by several chunks.  Global coordinates and global point IDs are
carried through untouched; each chunk emits per-point *votes* and the merge
step resolves disagreements with the explicit, documented rule set in
:func:`merge_votes`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .geometry import local_shape_descriptors
from .ransac import RansacConfig, RansacResult, ransac_plane

LABEL_GROUND = "ground"
LABEL_NON_GROUND = "non_ground"
LABEL_UNKNOWN = "unknown"

CONF_STRONG = "strong"
CONF_WEAK = "weak"


@dataclass(frozen=True)
class ChunkConfig:
    """Spatial chunking configuration (metres)."""

    chunk_size: float = 4.0
    overlap: float = 0.25            # fraction of chunk_size on every edge
    min_points: int = 12
    normal_k: int = 8
    cos_normal_strong: float = 0.90   # ~25.8 deg between normals
    cos_normal_weak: float = 0.75     # ~41.4 deg
    vertical_normal_z: float = 0.35   # |n_z| below this -> vertical feature
    inner_distance_fraction: float = 0.5
    non_ground_margin_fraction: float = 2.0
    enable_local_normals: bool = True
    max_points_per_chunk: int = 8000  # safety cap for the O(N*k) normal step


@dataclass
class Vote:
    """One chunk's classification of one global point."""

    point_id: int
    label: str
    confidence: str
    support: float
    chunk_id: str
    distance: float
    tilt_deg: float


@dataclass
class ChunkReport:
    """Diagnostics for a single spatial chunk."""

    chunk_id: str
    point_count: int
    status: str
    reason: str
    inlier_count: int
    inlier_ratio: float
    tilt_deg: float | None
    rms: float | None
    iterations_used: int


def _window_starts(min_v: float, max_v: float, size: float, overlap: float) -> np.ndarray:
    """Window start coordinates whose union covers ``[min_v, max_v]``.

    Windows have fixed width ``size`` and advance by ``size*(1-overlap)``.
    A final window is appended flush to ``max_v`` whenever the regular grid
    would leave an uncovered tail, so the union is always gap-free (windows
    may overlap more than nominal, never less).
    """
    if max_v <= min_v:
        return np.array([min_v], dtype=np.float64)
    stride = size * (1.0 - overlap)
    starts = [min_v]
    pos = min_v
    while pos + size < max_v - 1e-12:
        pos += stride
        starts.append(pos)
    starts_arr = np.asarray(starts, dtype=np.float64)
    # Flush the last window to the upper bound; drop it if it duplicates the
    # previous one (cloud exactly one stride-window wide).
    last = max_v - size
    if last > starts_arr[-1] + 1e-9:
        starts_arr = np.append(starts_arr, last)
    elif len(starts_arr) > 1 and abs(last - starts_arr[-1]) <= 1e-9 and \
            starts_arr[-1] - starts_arr[-2] < 1e-9:
        starts_arr = starts_arr[:-1]
    return starts_arr


def build_chunks(
    points: np.ndarray, cfg: ChunkConfig
) -> list[tuple[str, np.ndarray, tuple[float, float, float, float]]]:
    """Return ``[(chunk_id, global_point_indices, (x0,y0,x1,y1)), ...]``.

    Overlapping windows are inclusive on both edges; a point therefore appears
    in every window whose footprint contains it (usually 1-4 chunks).
    """
    pts = np.asarray(points, dtype=np.float64)
    if len(pts) == 0:
        return []
    xmin, ymin = pts[:, 0].min(), pts[:, 1].min()
    xmax, ymax = pts[:, 0].max(), pts[:, 1].max()
    size = cfg.chunk_size
    if xmax - xmin <= size and ymax - ymin <= size:
        return [("c00", np.arange(len(pts)),
                 (float(xmin), float(ymin), float(xmax), float(ymax)))]

    xs = _window_starts(float(xmin), float(xmax), size, cfg.overlap)
    ys = _window_starts(float(ymin), float(ymax), size, cfg.overlap)
    chunks: list[tuple[str, np.ndarray, tuple[float, float, float, float]]] = []
    for iy, y0 in enumerate(ys):
        y1 = y0 + size
        for ix, x0 in enumerate(xs):
            x1 = x0 + size
            in_chunk = (
                (pts[:, 0] >= x0 - 1e-9)
                & (pts[:, 0] <= x1 + 1e-9)
                & (pts[:, 1] >= y0 - 1e-9)
                & (pts[:, 1] <= y1 + 1e-9)
            )
            ids = np.flatnonzero(in_chunk)
            if len(ids):
                chunks.append((f"c{iy:02d}_{ix:02d}", ids,
                               (float(x0), float(y0), float(x1), float(y1))))
    return chunks


def _support(ran_cfg: RansacConfig, ratio: float, rms: float) -> float:
    """Scalar vote weight in ``[0, 1]``: plane support and tightness.

    Combines the inlier ratio with an exponential roughness penalty (RMS
    normalised by the distance threshold).  Used only to break *strong vs
    strong* / *weak vs weak* ties; label agreement never depends on it.
    """
    ratio_part = min(max(ratio, 0.0), 1.0)
    tightness = math.exp(-max(rms, 0.0) / max(ran_cfg.distance_threshold, 1e-9))
    return float(0.7 * ratio_part + 0.3 * tightness)


def classify_chunk(
    points: np.ndarray,
    global_ids: np.ndarray,
    chunk_id: str,
    ran_cfg: RansacConfig,
    chunk_cfg: ChunkConfig,
) -> tuple[list[Vote], ChunkReport, RansacResult]:
    """Fit one chunk and emit a vote for each of its member points."""
    ids = np.asarray(global_ids, dtype=np.int64)
    pts = np.asarray(points, dtype=np.float64)
    result = ransac_plane(pts, ran_cfg)
    st = result.stats
    report = ChunkReport(
        chunk_id=chunk_id,
        point_count=len(pts),
        status=result.status,
        reason=result.reason,
        inlier_count=int(st.get("inlier_count", 0)),
        inlier_ratio=float(st.get("inlier_ratio", 0.0)),
        tilt_deg=st.get("tilt_deg"),
        rms=st.get("rms"),
        iterations_used=int(st.get("iterations_used", 0)),
    )
    if not result.reliable or result.plane is None:
        return [], report, result

    plane = result.plane
    dist = plane.signed_distance(pts)
    absd = np.abs(dist)
    thr = ran_cfg.distance_threshold
    inner = thr * chunk_cfg.inner_distance_fraction
    far = thr * chunk_cfg.non_ground_margin_fraction
    support = _support(ran_cfg, result.plane.inlier_ratio, result.plane.rms)

    if chunk_cfg.enable_local_normals and len(pts) <= chunk_cfg.max_points_per_chunk:
        normals, valid, linear = local_shape_descriptors(
            pts, k=chunk_cfg.normal_k, orientation="up"
        )
    else:
        normals = np.zeros_like(pts)
        valid = np.zeros(len(pts), dtype=bool)
        linear = np.zeros(len(pts), dtype=bool)
    cos_where = valid
    cosines = np.where(cos_where, normals @ plane.normal, 0.0)

    # Verticality of each local normal: a wall/pole neighbourhood has a
    # normal pointing nearly horizontally even when the point itself lands
    # inside the ground band on a sloping plane.
    verticality = np.where(valid, np.abs(normals[:, 2]), 1.0)

    votes: list[Vote] = []
    for local_i, gid in enumerate(ids):
        d = float(absd[local_i])
        if d <= thr:
            if valid[local_i]:
                c = float(cosines[local_i])
                vert = float(verticality[local_i])
                if c >= chunk_cfg.cos_normal_strong:
                    conf = CONF_STRONG
                elif c >= chunk_cfg.cos_normal_weak:
                    conf = CONF_WEAK
                elif vert <= chunk_cfg.vertical_normal_z or d > inner:
                    # Local surface faces a structurally different direction:
                    # either an explicit vertical feature (wall/pole caught
                    # inside the band on a slope) or a point near the band
                    # edge with a sideways normal.  Both are non-ground.
                    votes.append(Vote(int(gid), LABEL_NON_GROUND, CONF_STRONG,
                                      support, chunk_id, float(dist[local_i]),
                                      plane.tilt_deg))
                    continue
                else:
                    # Inconsistent but non-vertical normal deep in the band:
                    # more likely noisy neighbourhood than a wall; keep a weak
                    # ground vote and let overlap merge arbitrate.
                    conf = CONF_WEAK
            elif linear[local_i] and d > inner:
                # Within the band but the neighbourhood is 1-D (wall row or
                # pole column intersecting the fitted ground plane): a planar
                # ground point never has a line-shaped neighbourhood.
                votes.append(Vote(int(gid), LABEL_NON_GROUND, CONF_STRONG,
                                  support, chunk_id, float(dist[local_i]),
                                  plane.tilt_deg))
                continue
            else:
                # No local descriptor (sparse / duplicated neighbourhood):
                # trust distance only when well inside the band.
                conf = CONF_STRONG if d <= inner else CONF_WEAK
            votes.append(Vote(int(gid), LABEL_GROUND, conf, support, chunk_id,
                              float(dist[local_i]), plane.tilt_deg))
        else:
            conf = CONF_STRONG if d >= far else CONF_WEAK
            votes.append(Vote(int(gid), LABEL_NON_GROUND, conf, support, chunk_id,
                              float(dist[local_i]), plane.tilt_deg))
    return votes, report, result


def merge_votes(votes: list[Vote]) -> tuple[str, str, dict]:
    """Resolve all votes cast for a single point.

    Explicit conflict rules (in order):

    1. No votes / all chunks undecidable on this point  -> ``unknown``.
    2. Votes agree on the label                         -> that label,
       confidence = the strongest cast, support = max support.
    3. Labels conflict and confidence tiers differ     -> the ``strong``
       side wins over the ``weak`` side.
    4. Labels conflict at the same tier                 -> the side with the
       larger total support wins; a tie (< ``SUPPORT_EPS``) stays
       ``unknown`` — we never guess when evidence is balanced.
    """
    SUPPORT_EPS = 1e-6
    if not votes:
        return LABEL_UNKNOWN, CONF_WEAK, {"vote_count": 0, "reason": "no_votes"}

    def side(label: str) -> dict[str, list[Vote]]:
        strong = [v for v in votes if v.label == label and v.confidence == CONF_STRONG]
        weak = [v for v in votes if v.label == label and v.confidence == CONF_WEAK]
        return {"strong": strong, "weak": weak}

    g, ng = side(LABEL_GROUND), side(LABEL_NON_GROUND)
    has_g = bool(g["strong"] or g["weak"])
    has_ng = bool(ng["strong"] or ng["weak"])

    if has_g and not has_ng:
        label = LABEL_GROUND
    elif has_ng and not has_g:
        label = LABEL_NON_GROUND
    elif not has_g and not has_ng:
        return LABEL_UNKNOWN, CONF_WEAK, {"vote_count": len(votes),
                                          "reason": "all_unknown"}
    else:
        # Conflict.
        if g["strong"] and not ng["strong"]:
            label = LABEL_GROUND
        elif ng["strong"] and not g["strong"]:
            label = LABEL_NON_GROUND
        else:
            # Same-tier conflict: strong vs strong, or weak vs weak.
            use_strong = bool(g["strong"] and ng["strong"])
            pool_g = g["strong"] if use_strong else g["weak"]
            pool_ng = ng["strong"] if use_strong else ng["weak"]
            sup_g = sum(v.support for v in pool_g)
            sup_ng = sum(v.support for v in pool_ng)
            if abs(sup_g - sup_ng) <= SUPPORT_EPS:
                return LABEL_UNKNOWN, CONF_WEAK, {
                    "vote_count": len(votes),
                    "reason": "support_tie",
                    "support_ground": sup_g,
                    "support_non_ground": sup_ng,
                }
            label = LABEL_GROUND if sup_g > sup_ng else LABEL_NON_GROUND

    chosen = [v for v in votes if v.label == label]
    confidence = CONF_STRONG if any(v.confidence == CONF_STRONG for v in chosen) else CONF_WEAK
    return label, confidence, {
        "vote_count": len(votes),
        "support_ground": sum(v.support for v in votes if v.label == LABEL_GROUND),
        "support_non_ground": sum(v.support for v in votes if v.label == LABEL_NON_GROUND),
        "chunks": sorted({v.chunk_id for v in votes}),
    }


@dataclass
class SegmentationOutput:
    """Final per-point result plus diagnostics."""

    labels: list[str]
    confidences: list[str]
    point_ids: list[int]
    merge_info: list[dict]
    chunk_reports: list[ChunkReport] = field(default_factory=list)
    reliable_chunk_count: int = 0
    undecidable_chunk_count: int = 0


def segment_cloud(
    points: np.ndarray,
    ransac_cfg: RansacConfig | None = None,
    chunk_cfg: ChunkConfig | None = None,
) -> SegmentationOutput:
    """Chunk -> seeded RANSAC -> normal/distance votes -> confidence merge."""
    ransac_cfg = ransac_cfg or RansacConfig()
    chunk_cfg = chunk_cfg or ChunkConfig()
    pts = np.asarray(points, dtype=np.float64)
    n = len(pts)

    all_votes: list[Vote] = []
    chunk_reports: list[ChunkReport] = []
    reliable = 0
    for chunk_id, gids, _bounds in build_chunks(pts, chunk_cfg):
        if len(gids) < chunk_cfg.min_points:
            chunk_reports.append(ChunkReport(
                chunk_id=chunk_id, point_count=len(gids), status="skipped",
                reason="below_min_points", inlier_count=0, inlier_ratio=0.0,
                tilt_deg=None, rms=None, iterations_used=0))
            continue
        votes, report, _ = classify_chunk(
            pts[gids], gids, chunk_id, ransac_cfg, chunk_cfg
        )
        all_votes.extend(votes)
        chunk_reports.append(report)
        if report.status == "reliable":
            reliable += 1

    by_point: dict[int, list[Vote]] = {}
    for v in all_votes:
        by_point.setdefault(v.point_id, []).append(v)

    labels: list[str] = []
    confidences: list[str] = []
    merge_info: list[dict] = []
    for pid in range(n):
        label, conf, info = merge_votes(by_point.get(pid, []))
        labels.append(label)
        confidences.append(conf)
        merge_info.append(info)

    return SegmentationOutput(
        labels=labels,
        confidences=confidences,
        point_ids=list(range(n)),
        merge_info=merge_info,
        chunk_reports=chunk_reports,
        reliable_chunk_count=reliable,
        undecidable_chunk_count=sum(
            1 for r in chunk_reports if r.status != "reliable"
        ),
    )
