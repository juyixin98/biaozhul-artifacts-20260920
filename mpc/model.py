"""Double-integrator dynamics, derived from the continuous-time model.

Continuous model (state x = [position, velocity], input u = acceleration):

    p_dot = v
    v_dot = u

i.e. x_dot = A_c x + B_c u with

        A_c = [[0, 1],
               [0, 0]],      B_c = [[0],
                                    [1]]

Zero-order-hold exact discretization with period dt is computed *from the
model* by truncating the matrix exponential series; for this nilpotent A_c
the series terminates after the second-order term, giving the familiar
exact result:

        A_d = [[1, dt],
               [0,  1]],     B_d = [[0.5*dt^2],
                                    [dt]]

A reference simulator propagates a controlled state with an optional
deterministic additive disturbance.
"""

from __future__ import annotations

import math

import numpy as np

from .config import MPCConfig


class DoubleIntegrator:
    """ZOH-discretized double integrator built from A_c, B_c."""

    NX = 2
    NU = 1

    def __init__(self, config: MPCConfig):
        self.config = config
        dt = config.dt

        # Continuous-time model matrices.
        self.Ac = np.array([[0.0, 1.0],
                            [0.0, 0.0]])
        self.Bc = np.array([[0.0],
                            [1.0]])

        # Exact ZOH discretization via matrix-exponential series evaluated
        # from the model (A_c is nilpotent of index 2).
        self.Ad, self.Bd = self._zoh_discretize(dt)

    def _zoh_discretize(self, dt: float):
        Ac, Bc = self.Ac, self.Bc
        n = Ac.shape[0]

        # A_d = exp(A_c dt) = I + A_c dt + (A_c dt)^2/2! + ...
        M = Ac * dt
        Ad = np.eye(n)
        term = np.eye(n)
        for k in range(1, 32):
            term = term @ M / k
            Ad = Ad + term
            if np.max(np.abs(term)) < 1e-15:
                break

        # B_d = integral_0^dt exp(A_c tau) d tau * B_c, evaluated by the
        # same series: sum_{k=0}^inf A_c^k dt^{k+1}/(k+1)! * B_c.
        Bd = np.zeros((n, Bc.shape[1]))
        A_power = np.eye(n)
        for k in range(0, 32):
            coeff = dt ** (k + 1) / math.factorial(k + 1)
            Bd = Bd + coeff * (A_power @ Bc)
            A_power = A_power @ Ac

        return Ad, Bd

    def step(self, x: np.ndarray, u: float,
             disturbance: np.ndarray | None = None) -> np.ndarray:
        """Propagate one controlled step: x+ = A_d x + B_d u + w."""
        x = np.asarray(x, dtype=float).reshape(self.NX)
        x_next = self.Ad @ x + self.Bd.reshape(self.NX) * float(u)
        if disturbance is not None:
            x_next = x_next + np.asarray(disturbance, dtype=float).reshape(self.NX)
        return x_next
