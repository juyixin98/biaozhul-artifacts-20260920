"""End-to-end scene tests: precision/recall vs constructed truth."""

import numpy as np
import pytest

from app.segmentation.chunking import (
    LABEL_GROUND,
    LABEL_NON_GROUND,
    LABEL_UNKNOWN,
    ChunkConfig,
    segment_cloud,
)
from app.segmentation.metrics import evaluate_report
from app.segmentation.ransac import RansacConfig
from app.segmentation.synth import (
    SCENES,
    build_scene,
)

DECIDABLE = ["flat_with_wall", "slope", "moderate_noise", "duplicates",
             "mixed_world"]
UNKNOWABLE = ["steep_slope", "sparse", "noise_dominated"]


def _run(name):
    scene = build_scene(name)
    out = segment_cloud(np.asarray(scene.points, dtype=np.float64),
                        RansacConfig(), ChunkConfig())
    return scene, out, evaluate_report(out.labels, scene.labels)


@pytest.mark.parametrize("name", DECIDABLE)
def test_decidable_scenes_meet_precision_recall(name):
    scene, out, rep = _run(name)
    g = rep.ground
    assert g.precision >= 0.95, (name, g)
    assert g.recall >= 0.95, (name, g)
    # Non-ground side must be accurate too (walls/poles/noise).
    ng = rep.non_ground
    assert ng.precision >= 0.90, (name, ng)
    assert ng.recall >= 0.90, (name, ng)
    assert rep.unknown_predictions == 0


@pytest.mark.parametrize("name", UNKNOWABLE)
def test_unknowable_scenes_are_abstained(name):
    scene, out, rep = _run(name)
    # No point receives a definite label; the biggest plane is never forced
    # to be ground just because it fits.
    assert rep.unknown_predictions == len(scene.points)
    assert set(out.labels) <= {LABEL_UNKNOWN}


def test_wall_never_ground_in_flat_scene():
    scene, out, _ = _run("flat_with_wall")
    wall_ids = [i for i, lab in enumerate(scene.labels) if lab == LABEL_NON_GROUND]
    assert wall_ids
    assert all(out.labels[i] == LABEL_NON_GROUND for i in wall_ids)


def test_duplicates_get_consistent_labels():
    scene, out, _ = _run("duplicates")
    coords = {}
    # Group labels by exact coordinate: duplicate rows must agree.
    for lab, p in zip(out.labels, scene.points):
        key = tuple(np.round(p, 9))
        coords.setdefault(key, set()).add(lab)
    for key, labs in coords.items():
        assert len(labs) == 1, (key, labs)


def test_scene_coverage_all_registered():
    assert set(SCENES) == set(DECIDABLE + UNKNOWABLE)
