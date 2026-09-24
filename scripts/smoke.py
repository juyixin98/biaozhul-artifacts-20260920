"""Quick smoke check (not part of pytest): runs the two hard fixtures."""
import numpy as np

from app.geometry import Rect, verify_trajectory
from app.smoother import SmoothConfig, smooth_path


def show(name, pts, rects, cfg, clearance):
    res = smooth_path(np.array(pts, dtype=float), rects, cfg)
    scale = res.scale
    edge = max(np.hypot(*(np.array(pts)[1:] - np.array(pts)[:-1]).T))
    spacing = max((clearance / scale) / 2.0 if clearance > 0 else 0.0,
                  edge / 200) * scale
    ver = verify_trajectory(res.points, rects, clearance,
                            spacing if spacing > 0 else 1e-3)
    obs = res.residuals.get("observed_physical", {})
    print(f"== {name}: {res.status} | iters={res.iterations} | "
          f"obj={res.objective:.6g} | max_dev={obs.get('max_deviation')} "
          f"| max_kappa={obs.get('max_curvature')} | min_clear={ver['min_clearance']} "
          f"| samples={ver['samples_checked']} | collapsed={res.collapsed_duplicates}")
    print("   ", res.message)
    return res


# Fixture 1: narrow corridor — walls leave a ~0.4-wide channel around y=0
P1 = [(0, 0), (2, 0.15), (4, -0.15), (6, 0.15), (8, -0.15), (10, 0.15), (12, 0)]
walls1 = [
    Rect.from_center_wh(2, 1.2, 4, 2.0),
    Rect.from_center_wh(6, 1.2, 4, 2.0),
    Rect.from_center_wh(10, 1.2, 4, 2.0),
    Rect.from_center_wh(2, -1.2, 4, 2.0),
    Rect.from_center_wh(6, -1.2, 4, 2.0),
    Rect.from_center_wh(10, -1.2, 4, 2.0),
]
show("narrow-corridor", P1, walls1,
     SmoothConfig(curvature_cap=0.8, corridor=0.45, clearance=0.05,
                  max_iterations=200), 0.05)

# Fixture 2: corner cutting — original L-path goes AROUND the block; the
# naive straight chord from start to goal cuts straight through it.
P2 = [(0, 0), (5, 0), (8, 0), (8, 5), (8, 10), (5, 10), (10, 10)]
block = [Rect.from_center_wh(5, 5, 4.0, 4.0)]
show("corner-cut", P2, block,
     SmoothConfig(curvature_cap=0.8, corridor=1.5, clearance=0.05,
                  max_iterations=200), 0.05)
