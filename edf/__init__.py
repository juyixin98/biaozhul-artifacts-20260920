"""edf - exact Euclidean distance field for 2D occupancy grids.

Pure offline library: given a boolean occupancy grid and a (possibly
non-square) cell size, compute for every cell the exact Euclidean distance
to the nearest occupied (obstacle) cell and the source cell that attains it.
"""

from .distance_field import distance_field
from .brute_force import brute_force_field

__all__ = ["distance_field", "brute_force_field"]
__version__ = "0.1.0"
