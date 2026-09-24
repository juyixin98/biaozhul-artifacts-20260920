"""2D occupancy grid backend (log-odds, synthetic/offline data only)."""

from .grid import CellState, GridConfig, OccupancyGrid
from .raycast import RayObservation, traverse_cells

__all__ = [
    "CellState",
    "GridConfig",
    "OccupancyGrid",
    "RayObservation",
    "traverse_cells",
]
