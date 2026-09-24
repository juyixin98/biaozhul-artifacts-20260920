"""Save / load occupancy grid state as a compressed .npz archive.

The archive stores every parameter needed to reconstruct the grid plus
the raw log-odds array, so a save/load round-trip preserves the state
bit-for-bit (float64 arrays are written uncompressed inside the zip).
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

import numpy as np

from .grid import OccupancyGrid

FORMAT_VERSION = 1


def save_grid(grid: OccupancyGrid, path: Union[str, Path]) -> Path:
    """Write the grid to ``path`` (``.npz`` appended if missing)."""
    path = Path(path)
    if path.suffix != ".npz":
        path = path.with_suffix(".npz")
    np.savez(
        path,
        format_version=np.int64(FORMAT_VERSION),
        width=np.int64(grid.width),
        height=np.int64(grid.height),
        resolution=np.float64(grid.resolution),
        origin_x=np.float64(grid.origin_x),
        origin_y=np.float64(grid.origin_y),
        l_occ=np.float64(grid.l_occ),
        l_free=np.float64(grid.l_free),
        l_min=np.float64(grid.l_min),
        l_max=np.float64(grid.l_max),
        log_odds=grid.log_odds,
    )
    return path


def load_grid(path: Union[str, Path]) -> OccupancyGrid:
    """Load a grid previously written by :func:`save_grid`."""
    path = Path(path)
    with np.load(path) as data:
        version = int(data["format_version"])
        if version != FORMAT_VERSION:
            raise ValueError(f"unsupported grid format version: {version}")
        grid = OccupancyGrid(
            width=int(data["width"]),
            height=int(data["height"]),
            resolution=float(data["resolution"]),
            origin_x=float(data["origin_x"]),
            origin_y=float(data["origin_y"]),
            l_occ=float(data["l_occ"]),
            l_free=float(data["l_free"]),
            l_min=float(data["l_min"]),
            l_max=float(data["l_max"]),
        )
        loaded = np.asarray(data["log_odds"], dtype=np.float64)
        if loaded.shape != grid.log_odds.shape:
            raise ValueError(
                f"log_odds shape {loaded.shape} does not match "
                f"grid dimensions {grid.log_odds.shape}"
            )
        grid.log_odds[...] = loaded
    return grid
