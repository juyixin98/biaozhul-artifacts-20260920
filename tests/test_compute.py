"""Unit tests for the deterministic experiment pipeline."""

from __future__ import annotations

import pytest

from app.compute import BagFormatError, parse_bag, run_pipeline
from app.crypto import canonical_json

MATRIX = [
    [1.0, 0.0, 0.0, 0.05],
    [0.0, 1.0, 0.0, -0.02],
    [0.0, 0.0, 1.0, 0.10],
    [0.0, 0.0, 0.0, 1.0],
]
BAG = b"0.1,0.2,1.0,1.0\n0.3,0.4,1.1,0.9\n0.5,0.6,1.2,1.0\n"
PARAMS = {"gain": 1.5, "offset": 0.01, "filter_width": 2.0}


def test_parse_bag_comments_and_blanks():
    pts = parse_bag(b"# header\n\n0.1,0.2,1.0,1.0\n  # c\n0.3,0.4,1.1\n")
    assert len(pts) == 2
    assert pts[1].w == 1.0  # default weight


@pytest.mark.parametrize(
    "bad",
    [
        b"0.1,0.2\n",          # too few coords
        b"a,b,c\n",            # non-numeric
        b"inf,0.0,0.0\n",      # non-finite
        b"# only comments\n",  # no records
    ],
)
def test_parse_bag_rejects_bad_input(bad):
    with pytest.raises(BagFormatError):
        parse_bag(bad)


def test_pipeline_is_deterministic():
    r1 = run_pipeline(BAG, PARAMS, MATRIX, 42)
    r2 = run_pipeline(BAG, PARAMS, MATRIX, 42)
    assert canonical_json(r1) == canonical_json(r2)


def test_different_seed_changes_result():
    r1 = run_pipeline(BAG, PARAMS, MATRIX, 1)
    r2 = run_pipeline(BAG, PARAMS, MATRIX, 2)
    assert r1 != r2
    # Geometry stays identical across seeds (centroid is seed-independent);
    # only the seeded score/jitter term varies.
    assert r1["centroid"] == r2["centroid"]
    assert r1["score"] != r2["score"]


def test_param_and_calibration_changes_result():
    base = run_pipeline(BAG, PARAMS, MATRIX, 7)
    assert run_pipeline(BAG, {**PARAMS, "gain": 9.0}, MATRIX, 7) != base
    other_matrix = [row[:] for row in MATRIX]
    other_matrix[0][3] = 5.0
    assert run_pipeline(BAG, PARAMS, other_matrix, 7) != base


def test_bag_change_changes_result():
    assert run_pipeline(BAG, PARAMS, MATRIX, 7) != run_pipeline(
        BAG + b"9.9,9.9,9.9,1.0\n", PARAMS, MATRIX, 7
    )
