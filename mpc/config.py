"""Configuration for the bounded double-integrator MPC.

All quantities are in abstract SI-consistent units (position m, velocity m/s,
acceleration m/s^2, time s). The constraints below are symmetric and hard:

* |v| <= v_max            (velocity bound)
* |a| <= a_max            (acceleration / input bound)

The optimizer penalizes tracking error and *control change* over a fixed
horizon N:

    sum_{k=0}^{N-1} [ q * ||x_k - r_k||^2 + r_d * (u_k - u_{k-1})^2 ]
                        + q_N * ||x_N - r_N||^2
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MPCConfig:
    # --- discretization ---
    dt: float = 0.1

    # --- horizon ---
    horizon: int = 20

    # --- cost weights ---
    q_pos: float = 10.0       # position tracking weight
    q_vel: float = 1.0        # velocity tracking weight
    q_term_pos: float = 50.0  # terminal position weight
    q_term_vel: float = 5.0   # terminal velocity weight
    r_delta: float = 0.5      # control-change weight (u_k - u_{k-1})

    # --- hard constraints ---
    v_max: float = 2.0        # |velocity| <= v_max
    a_max: float = 3.0        # |acceleration| <= a_max

    # --- solver ---
    osqp_max_iter: int = 20000
    osqp_eps_abs: float = 1e-6
    osqp_eps_rel: float = 1e-6
    osqp_time_limit: float = 0.5  # seconds; enforces the solve timeout
    osqp_polish: bool = False
    osqp_verbose: bool = False

    # --- conservative fallback controller ---
    # Braking control law: u_fb = -K_brake * v, then saturated at a_max.
    # K_brake = 2*v_max/dt guarantees a full-speed state can be driven to the
    # bound in one step; the smaller default still decelerates hard while
    # staying strictly inside |a| <= a_max for every feasible |v| <= v_max.
    k_brake: float = 1.0

    def __post_init__(self) -> None:
        if self.dt <= 0:
            raise ValueError("dt must be positive")
        if self.horizon < 1:
            raise ValueError("horizon must be >= 1")
        if min(self.q_pos, self.q_vel, self.q_term_pos,
               self.q_term_vel, self.r_delta) < 0:
            raise ValueError("weights must be non-negative")
        if self.v_max <= 0 or self.a_max <= 0:
            raise ValueError("bounds must be positive")
        if self.osqp_time_limit <= 0:
            raise ValueError("osqp_time_limit must be positive")
        # Braking gain must keep the fallback saturated inside a_max for every
        # feasible velocity: K_brake * v_max <= a_max.
        if self.k_brake * self.v_max > self.a_max + 1e-12:
            raise ValueError(
                "k_brake too large: fallback would exceed a_max at v_max"
            )
