"""Offline replay demo: synthesize a transform log, replay it into the tree,
and query it back -- no hardware, no visualization.

Run with:  python examples/replay_demo.py
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tf_cache import SE3, ExtrapolationError, TransformTree


def synthesize_log(duration=2.0, rate_hz=10.0):
    """Simulate a robot: world->base drives in a straight line while
    base->turret pans 0 -> 90 deg about z. Returns a list of samples."""
    samples = []
    n = int(duration * rate_hz) + 1
    for i in range(n):
        t = i / rate_hz
        frac = t / duration
        # world -> base: pure translation along x, 1 m/s.
        samples.append(("world", "base", t, [0, 0, 0, 1], [frac * duration, 0.0, 0.0]))
        # base -> turret: pan about z plus a fixed lift.
        angle = frac * np.pi / 2.0
        q = [0.0, 0.0, np.sin(angle / 2.0), np.cos(angle / 2.0)]
        samples.append(("base", "turret", t, q, [0.0, 0.0, 0.5]))
    return samples


def main():
    log = synthesize_log()
    tree = TransformTree()
    for parent, child, t, quat, trans in log:
        tree.set_transform(parent, child, t, SE3.from_quat_translation(quat, trans))
    print(f"replayed {len(log)} samples; frames: {tree.frames}")

    # Query the turret pose in world coordinates halfway through the log.
    t_query = 1.0
    tf = tree.lookup("turret", "world", t_query)
    print(f"\nturret in world @ t={t_query}:")
    print(f"  translation: {np.round(tf.translation, 6).tolist()}")
    print(f"  quaternion (xyzw): {np.round(tf.quat_xyzw(), 6).tolist()}")
    # Ground truth at t=1.0: base at x=1.0, turret panned 45 deg, lifted 0.5.
    print("  expected:      [1.0, 0.0, 0.5], 45 deg about z")

    # Round-trip check: compose with the inverse chain.
    back = tree.lookup("world", "turret", t_query)
    d_trans, d_rot = (tf * back).error_to(SE3.identity())
    print(f"\nround-trip error: translation={d_trans:.3e} m, rotation={d_rot:.3e} rad")

    # Extrapolation is refused.
    try:
        tree.lookup("turret", "world", 99.0)
    except ExtrapolationError as exc:
        print(f"\nextrapolation correctly refused: {exc}")


if __name__ == "__main__":
    main()
