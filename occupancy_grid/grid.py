"""2D occupancy grid with log-odds Bayesian updates.

State model
-----------
Each cell stores a log-odds value ``L = logit(p)`` starting from the prior
``logit(p_prior)`` (0.0 for the default uniform prior ``p_prior = 0.5``).
Every ray observation adds a fixed log-odds increment:

* free (traversed) cell:  ``L += logit(p_free)``  (negative)
* occupied (echo) cell:   ``L += logit(p_occ)``   (positive)

After every update the value is clamped to ``[logit(p_clamp_min),
logit(p_clamp_max)]``; by default the bounds are symmetric ``logit(0.01)`` /
``logit(0.99)`` so log-odds stay finite (``sigmoid`` never returns exactly 0 or
1) while repeated identical observations accumulate to saturation.

Classification uses strict comparisons (exact ties are possible because
``logit(0.01) = -logit(0.99)``):

* ``L > occ_threshold``  -> occupied
* ``L < free_threshold`` -> free
* otherwise              -> unknown

Default thresholds are 0.0 / 0.0, i.e. a single ray decides a cell; a freshly
initialised grid (all zeros) is entirely unknown.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass

import numpy as np
from scipy.special import expit

from .raycast import RayObservation, traverse_cells


class CellState(str, enum.Enum):
    UNKNOWN = "unknown"
    FREE = "free"
    OCCUPIED = "occupied"


@dataclass(frozen=True)
class GridConfig:
    """Map geometry and sensor model parameters."""

    nx: int
    ny: int
    resolution: float = 1.0
    origin_x: float = 0.0
    origin_y: float = 0.0
    p_occ: float = 0.7
    p_free: float = 0.3
    p_clamp_max: float = 0.99
    p_clamp_min: float = 0.01
    p_prior: float = 0.5
    occ_threshold: float = 0.0
    free_threshold: float = 0.0

    def __post_init__(self) -> None:
        if self.nx <= 0 or self.ny <= 0:
            raise ValueError("nx and ny must be positive")
        if self.resolution <= 0:
            raise ValueError("resolution must be positive")
        for name in ("p_occ", "p_free", "p_clamp_max", "p_clamp_min", "p_prior"):
            p = getattr(self, name)
            if not 0.0 < p < 1.0:
                raise ValueError(f"{name} must be strictly within (0, 1)")
        if self.p_occ <= 0.5:
            raise ValueError("p_occ must be > 0.5")
        if self.p_free >= 0.5:
            raise ValueError("p_free must be < 0.5")
        if self.p_clamp_min >= self.p_free or self.p_clamp_max <= self.p_occ:
            raise ValueError("clamping probabilities must not cut off sensor updates")
        if self.occ_threshold < self.free_threshold:
            raise ValueError("occ_threshold must be >= free_threshold")


def logit(p: float) -> float:
    return math.log(p / (1.0 - p))


class OccupancyGrid:
    """Log-odds occupancy grid indexed as ``log_odds[iy, ix]``."""

    SCHEMA_VERSION = 1

    def __init__(self, config: GridConfig):
        self.config = config
        self.l_occ = logit(config.p_occ)
        self.l_free = logit(config.p_free)
        self.l_max = logit(config.p_clamp_max)
        self.l_min = logit(config.p_clamp_min)
        self.log_odds = np.full(
            (config.ny, config.nx), logit(config.p_prior), dtype=np.float64
        )

    # ------------------------------------------------------------------ geo

    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        """Map continuous world coordinates to integer cell indices (floor)."""
        c = self.config
        ix = int(math.floor((x - c.origin_x) / c.resolution))
        iy = int(math.floor((y - c.origin_y) / c.resolution))
        return ix, iy

    def in_map(self, ix: int, iy: int) -> bool:
        return 0 <= ix < self.config.nx and 0 <= iy < self.config.ny

    def _to_cell_coords(self, x: float, y: float) -> tuple[float, float]:
        c = self.config
        return (x - c.origin_x) / c.resolution, (y - c.origin_y) / c.resolution

    # --------------------------------------------------------------- update

    def update_ray(self, ray: RayObservation) -> dict:
        """Integrate one ray observation and report what was updated.

        ``ray.endpoint`` given (or derivable from angle/max_range) -> echo;
        endpoint cell occupied, traversed cells free. Endpoint outside the map
        -> occupied update dropped, clipped free segment still applied.
        ``max_range`` with no endpoint direction -> error.
        """
        endpoint = ray.resolved_endpoint()
        if endpoint is None:
            raise ValueError(
                "ray needs (ex, ey) or (angle, max_range) to define an endpoint"
            )

        cx0, cy0 = self._to_cell_coords(ray.ox, ray.oy)
        cx1, cy1 = self._to_cell_coords(*endpoint)
        cells = traverse_cells(cx0, cy0, cx1, cy1, self.config.nx, self.config.ny)

        hit = self.world_to_cell(*endpoint)
        endpoint_in_map = self.in_map(*hit)

        occupied_cell = hit if endpoint_in_map else None
        # The endpoint cell gets the occupied update; every other in-map cell
        # on the traversed segment is free. For an out-of-map endpoint the
        # occupied update is dropped (clipping rule) and all clipped cells free.
        free_cells = [
            cell for cell in cells if cell != occupied_cell and self.in_map(*cell)
        ]

        for ix, iy in free_cells:
            self._apply(ix, iy, self.l_free)
        if occupied_cell is not None:
            self._apply(*occupied_cell, self.l_occ)

        return {
            "free_cells": free_cells,
            "occupied_cell": list(occupied_cell) if occupied_cell else None,
            "endpoint_in_map": endpoint_in_map,
        }

    def update_no_return(self, ox: float, oy: float, angle: float, max_range: float) -> dict:
        """Integrate a no-return ray: everything up to ``max_range`` is free."""
        if max_range <= 0:
            raise ValueError("max_range must be positive")
        ex = ox + max_range * math.cos(angle)
        ey = oy + max_range * math.sin(angle)
        cx0, cy0 = self._to_cell_coords(ox, oy)
        cx1, cy1 = self._to_cell_coords(ex, ey)
        cells = [
            c
            for c in traverse_cells(cx0, cy0, cx1, cy1, self.config.nx, self.config.ny)
            if self.in_map(*c)
        ]
        for ix, iy in cells:
            self._apply(ix, iy, self.l_free)
        return {"free_cells": cells, "occupied_cell": None, "endpoint_in_map": False}

    def _apply(self, ix: int, iy: int, delta: float) -> None:
        value = self.log_odds[iy, ix] + delta
        if value > self.l_max:
            value = self.l_max
        elif value < self.l_min:
            value = self.l_min
        self.log_odds[iy, ix] = value

    # ---------------------------------------------------------------- query

    def log_odds_at(self, ix: int, iy: int) -> float:
        if not self.in_map(ix, iy):
            raise IndexError(f"cell ({ix}, {iy}) is outside the map")
        return float(self.log_odds[iy, ix])

    def probability_at(self, ix: int, iy: int) -> float:
        return float(expit(self.log_odds_at(ix, iy)))

    def state_at(self, ix: int, iy: int) -> CellState:
        value = self.log_odds_at(ix, iy)
        c = self.config
        if value > c.occ_threshold:
            return CellState.OCCUPIED
        if value < c.free_threshold:
            return CellState.FREE
        return CellState.UNKNOWN

    def probability_map(self) -> np.ndarray:
        """Occupancy probability per cell, shape ``(ny, nx)``."""
        return expit(self.log_odds)

    def state_map(self) -> list[list[str]]:
        c = self.config
        states: list[list[str]] = []
        for iy in range(c.ny):
            row = []
            for ix in range(c.nx):
                row.append(self.state_at(ix, iy).value)
            states.append(row)
        return states

    # ---------------------------------------------------------- persistence

    def to_dict(self) -> dict:
        c = self.config
        return {
            "schema_version": self.SCHEMA_VERSION,
            "config": {
                "nx": c.nx,
                "ny": c.ny,
                "resolution": c.resolution,
                "origin_x": c.origin_x,
                "origin_y": c.origin_y,
                "p_occ": c.p_occ,
                "p_free": c.p_free,
                "p_clamp_max": c.p_clamp_max,
                "p_clamp_min": c.p_clamp_min,
                "p_prior": c.p_prior,
                "occ_threshold": c.occ_threshold,
                "free_threshold": c.free_threshold,
            },
            "log_odds": self.log_odds.tolist(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OccupancyGrid":
        if not isinstance(data, dict):
            raise ValueError("export data must be an object")
        version = data.get("schema_version")
        if version != cls.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {version!r}")
        config = GridConfig(**data["config"])
        grid = cls(config)
        values = np.asarray(data["log_odds"], dtype=np.float64)
        if values.shape != (config.ny, config.nx):
            raise ValueError(
                f"log_odds shape {values.shape} != ({config.ny}, {config.nx})"
            )
        if np.any(values < grid.l_min - 1e-12) or np.any(values > grid.l_max + 1e-12):
            raise ValueError("log_odds values exceed configured clamping bounds")
        grid.log_odds = values
        return grid
