"""Condensed QP construction for the bounded MPC.

Everything here is built *from the model matrices* A_d, B_d produced in
``model.py`` -- nothing about the double integrator (e.g. ``v_{k+1}=v_k+...``)
is hard-coded into the constraint or cost assembly.

Decision variable (delta-input formulation, penalizes control change):

    z = [Du_0, Du_1, ..., Du_{N-1}],   Du_k = u_k - u_{k-1},  u_{-1} = u_prev

Predicted states along the horizon:

    X = [x_0 ... x_N] = Phi x0 + Gamma (u_prev * 1 + L z)

with Phi the vertical stack of A_d^k, Gamma the block-lower-triangular
impulse-response matrix and L the N x N lower-triangular matrix of ones.

Quadratic program fed to OSQP:

    min 0.5 z^T P z + q^T z
    s.t. -a_max <= u_k <= a_max            (input, k=0..N-1)
         -v_max <= v_k <= v_max            (velocity, predicted k=1..N)

P, q are obtained by substituting the state prediction into the tracking
cost  sum (x_k-r_k)^T Q (x_k-r_k) + terminal + r_delta sum Du_k^2.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp

from .config import MPCConfig
from .model import DoubleIntegrator


@dataclass(frozen=True)
class QPMatrices:
    P: sp.csc_matrix
    q: np.ndarray
    A: sp.csc_matrix
    lower: np.ndarray
    upper: np.ndarray
    # reconstruction helpers
    L: sp.csc_matrix
    Phi: sp.csc_matrix
    Gamma: sp.csc_matrix


class QPBuilder:
    def __init__(self, model: DoubleIntegrator, config: MPCConfig):
        self.model = model
        self.config = config
        self.nx = model.NX
        self.nu = model.NU
        self.N = config.horizon

        self.Q = np.diag([config.q_pos, config.q_vel])
        self.QN = np.diag([config.q_term_pos, config.q_term_vel])

        self.Phi, self.Gamma, self.L = self._build_prediction_matrices()
        self.Qbar = sp.block_diag(
            [sp.csc_matrix(self.Q)] * self.N + [sp.csc_matrix(self.QN)],
            format="csc",
        )
        # velocity selector: rows pick the v-component of x_k for k=1..N
        self.Sv = self._velocity_selector()

        # constant pieces of P / q
        GL = self.Gamma @ self.L
        self.P = (GL.T @ self.Qbar @ GL +
                  config.r_delta * sp.eye(self.N)).tocsc()
        self.Phi_t = self.Phi.T
        self.GL_v = (self.Sv @ GL).tocsc()
        self.SvPhi = (self.Sv @ self.Phi).toarray()
        self.SvG1 = (self.Sv @ self.Gamma @
                     np.ones((self.N, 1))).reshape(self.N)

    def _build_prediction_matrices(self):
        """Phi (stack of A^k), Gamma (block lower-tri. impulse), L (tril 1)."""
        A, B = sp.csc_matrix(self.model.Ad), sp.csc_matrix(self.model.Bd)
        N, nx, nu = self.N, self.nx, self.nu

        # A_pow[k] = A^k, k = 0 .. N
        A_pow = [sp.eye(nx, format="csc")]
        for _ in range(N):
            A_pow.append(A_pow[-1] @ A)

        phi_rows = [A_pow[k] for k in range(N + 1)]

        gamma_blocks = [[None] * N for _ in range(N + 1)]
        zero = sp.csc_matrix((nx, nu))
        for i in range(N + 1):
            for j in range(N):
                # x_i depends on u_j through A^{i-1-j} B if j <= i-1
                gamma_blocks[i][j] = A_pow[i - 1 - j] @ B if j <= i - 1 else zero

        Phi = sp.vstack(phi_rows, format="csc")
        Gamma = sp.bmat(gamma_blocks, format="csc")
        L = sp.tril(sp.csr_matrix(np.ones((N, N))), format="csc")
        return Phi, Gamma, L

    def _velocity_selector(self) -> sp.csc_matrix:
        """N x ((N+1)*nx), row k selects velocity component of x_{k+1}."""
        rows, cols = [], []
        for k in range(self.N):
            state_block = k + 1  # x_1 .. x_N
            rows.append(k)
            cols.append(state_block * self.nx + 1)
        data = np.ones(self.N)
        return sp.csc_matrix(
            (data, (rows, cols)), shape=(self.N, (self.N + 1) * self.nx)
        )

    def build(self, x0: np.ndarray, reference: np.ndarray,
              u_prev: float) -> QPMatrices:
        """Assemble the OSQP QP for the current state/reference/u_prev."""
        x0 = np.asarray(x0, dtype=float).reshape(self.nx)
        reference = np.asarray(reference, dtype=float).reshape(-1, self.nx)
        if reference.shape[0] != self.N + 1:
            raise ValueError(
                f"reference must have {self.N + 1} rows, got {reference.shape[0]}"
            )

        r_vec = reference.reshape(-1)
        phi_x0 = self.Phi @ x0

        # q = L^T G^T Qbar (Phi x0 + G (u_prev 1) - r)
        const = phi_x0 + u_prev * (self.Gamma @ np.ones(self.N)) - r_vec
        q = self.L.T @ (self.Gamma.T @ self.Qbar @ const)

        # ---- constraints: U = u_prev 1 + L z ----
        ones = np.ones(self.N)
        # |u_k| <= a_max
        a_lower = -self.config.a_max - u_prev * ones
        a_upper = self.config.a_max - u_prev * ones

        # |v_k| <= v_max on predicted states x_1..x_N
        v_const = self.SvPhi @ x0 + u_prev * self.SvG1
        A_v = self.GL_v
        v_lower = -self.config.v_max - v_const
        v_upper = self.config.v_max - v_const

        A_con = sp.vstack([self.L, A_v], format="csc")
        lower = np.concatenate([a_lower, v_lower])
        upper = np.concatenate([a_upper, v_upper])

        # OSQP needs the *upper* triangle of a symmetric P.
        P = sp.triu(self.P, format="csc")

        return QPMatrices(
            P=P, q=np.asarray(q).reshape(-1), A=A_con,
            lower=lower, upper=upper,
            L=self.L, Phi=self.Phi, Gamma=self.Gamma,
        )

    def reconstruct(self, qp: QPMatrices, x0: np.ndarray,
                    z: np.ndarray, u_prev: float):
        """Return input sequence U and predicted states X from solution z."""
        x0 = np.asarray(x0, dtype=float).reshape(self.nx)
        du = np.asarray(z, dtype=float).reshape(self.N)
        u_seq = u_prev + qp.L @ du
        x_vec = qp.Phi @ x0 + qp.Gamma @ u_seq
        states = x_vec.reshape(self.N + 1, self.nx)
        return u_seq, states
