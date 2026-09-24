"""Chunk coverage, global ID preservation and the vote-merge rule set."""

import numpy as np
import pytest

from app.segmentation.chunking import (
    CONF_STRONG,
    CONF_WEAK,
    LABEL_GROUND,
    LABEL_NON_GROUND,
    LABEL_UNKNOWN,
    ChunkConfig,
    Vote,
    build_chunks,
    classify_chunk,
    merge_votes,
    segment_cloud,
)
from app.segmentation.ransac import RansacConfig


def test_chunks_cover_every_global_index():
    rng = np.random.default_rng(0)
    pts = rng.uniform(0, 10, size=(500, 3))
    cfg = ChunkConfig(chunk_size=4.0, overlap=0.25)
    chunks = build_chunks(pts, cfg)
    assert len(chunks) > 1
    covered = np.concatenate([ids for _id, ids, _b in chunks])
    assert np.array_equal(np.sort(np.unique(covered)), np.arange(len(pts)))
    # Overlap is real: some points belong to multiple chunks.
    assert len(covered) > len(pts)


def test_single_small_cloud_one_chunk():
    pts = np.random.default_rng(1).uniform(0, 2, size=(30, 3))
    chunks = build_chunks(pts, ChunkConfig(chunk_size=4.0))
    assert len(chunks) == 1
    assert chunks[0][0] == "c00"


def test_global_ids_are_preserved():
    rng = np.random.default_rng(2)
    xs, ys = np.meshgrid(np.arange(0.0, 10.0, 0.6), np.arange(0.0, 10.0, 0.6))
    pts = np.column_stack([xs.ravel(), ys.ravel(),
                           rng.normal(scale=0.01, size=xs.size)])
    cfg = ChunkConfig(chunk_size=4.0, overlap=0.25)
    chunks = build_chunks(pts, cfg)
    for chunk_id, ids, _ in chunks:
        votes, report, _ = classify_chunk(
            pts[ids], ids * 10 + 5,  # arbitrary non-contiguous global IDs
            chunk_id, RansacConfig(rng_seed=1), cfg
        )
        # Vote ids must be the exact ids handed in, never local indices.
        assert {v.point_id for v in votes} <= set(ids * 10 + 5)
        assert report.point_count == len(ids)


def test_merge_no_votes_is_unknown():
    label, conf, info = merge_votes([])
    assert label == LABEL_UNKNOWN
    assert info["reason"] == "no_votes"


def test_merge_unanimous_ground():
    votes = [Vote(0, LABEL_GROUND, CONF_STRONG, 0.9, "c00_00", 0.01, 3.0),
             Vote(0, LABEL_GROUND, CONF_WEAK, 0.8, "c00_01", 0.05, 3.0)]
    label, conf, info = merge_votes(votes)
    assert label == LABEL_GROUND
    assert conf == CONF_STRONG
    assert info["vote_count"] == 2


def test_merge_strong_beats_weak_conflict():
    votes = [Vote(1, LABEL_GROUND, CONF_WEAK, 0.9, "a", 0.08, 5.0),
             Vote(1, LABEL_NON_GROUND, CONF_STRONG, 0.5, "b", 0.4, 5.0)]
    label, _, _ = merge_votes(votes)
    assert label == LABEL_NON_GROUND
    # Reverse: strong ground beats weak non-ground even with lower support.
    votes = [Vote(1, LABEL_GROUND, CONF_STRONG, 0.5, "a", 0.02, 5.0),
             Vote(1, LABEL_NON_GROUND, CONF_WEAK, 0.99, "b", 0.15, 5.0)]
    assert merge_votes(votes)[0] == LABEL_GROUND


def test_merge_same_tier_support_break_and_tie_abstains():
    votes = [Vote(2, LABEL_GROUND, CONF_STRONG, 0.8, "a", 0.01, 5.0),
             Vote(2, LABEL_NON_GROUND, CONF_STRONG, 0.6, "b", 0.5, 5.0)]
    assert merge_votes(votes)[0] == LABEL_GROUND

    tie = [Vote(2, LABEL_GROUND, CONF_STRONG, 0.7, "a", 0.01, 5.0),
           Vote(2, LABEL_NON_GROUND, CONF_STRONG, 0.7, "b", 0.5, 5.0)]
    label, _, info = merge_votes(tie)
    assert label == LABEL_UNKNOWN
    assert info["reason"] == "support_tie"


def test_end_to_end_global_coordinates_across_chunks():
    # A flat ground plane located far from the origin: chunking must keep the
    # global coordinates, so the fitted plane is found despite the offset.
    rng = np.random.default_rng(4)
    xs, ys = np.meshgrid(np.arange(100.0, 110.0, 0.5),
                         np.arange(-50.0, -40.0, 0.5))
    pts = np.column_stack([xs.ravel(), ys.ravel(),
                           -20.0 + rng.normal(scale=0.01, size=xs.size)])
    out = segment_cloud(pts, RansacConfig(rng_seed=3),
                        ChunkConfig(chunk_size=4.0, overlap=0.25))
    assert out.point_ids == list(range(len(pts)))
    assert out.reliable_chunk_count >= 4
    assert all(lab == LABEL_GROUND for lab in out.labels)


def test_skipped_small_chunks_leave_points_unknown():
    rng = np.random.default_rng(5)
    pts = rng.uniform(0, 3, size=(6, 3))
    out = segment_cloud(pts, RansacConfig(min_points=12),
                        ChunkConfig(chunk_size=4.0, min_points=12))
    assert all(lab == LABEL_UNKNOWN for lab in out.labels)
    assert out.chunk_reports[0].status == "skipped"
