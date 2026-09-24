"""Helpers to build valid edge specs in tests."""

from __future__ import annotations

import numpy as np

from app.models import RotationSpec, TransformSpec
from app.se3 import exp_se3

I3 = np.eye(3)
_UNSET = object()


def iso_cov(trans_std: float, rot_std: float) -> list[list[float]]:
    return np.diag([trans_std**2] * 3 + [rot_std**2] * 3).tolist()


def edge(
    parent: str,
    child: str,
    *,
    translation=(0.0, 0.0, 0.0),
    rotation=None,
    trans_std: float = 1e-3,
    rot_std: float = 1e-3,
    cov=_UNSET,
    edge_id: str | None = None,
    quat=None,
    convention=None,
) -> TransformSpec:
    if rotation is None and quat is None:
        rotation = RotationSpec(kind="matrix", matrix=I3.tolist())
    elif quat is not None:
        rotation = RotationSpec(kind="quat_wxyz", quat_wxyz=list(quat))
    else:
        rotation = RotationSpec(kind="matrix", matrix=np.asarray(rotation).tolist())
    if cov is None:
        covariance = None
    elif cov is _UNSET:
        covariance = iso_cov(trans_std, rot_std)
    else:
        covariance = np.asarray(cov).tolist()
    return TransformSpec(
        parent_frame=parent,
        child_frame=child,
        translation=list(translation),
        rotation=rotation,
        covariance=covariance,
        edge_id=edge_id,
        convention=convention,
    )


def rotated_edge_z(
    parent: str,
    child: str,
    angle: float,
    *,
    translation=(0.0, 0.0, 0.0),
    trans_std: float = 1e-3,
    rot_std: float = 1e-3,
    edge_id=None,
) -> TransformSpec:
    R = exp_se3(np.r_[np.zeros(3), [0.0, 0.0, angle]])[:3, :3]
    return edge(
        parent,
        child,
        translation=translation,
        rotation=R,
        trans_std=trans_std,
        rot_std=rot_std,
        edge_id=edge_id,
    )
