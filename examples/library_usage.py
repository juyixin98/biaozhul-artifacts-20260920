"""库接口最小示例：一维恒速跟踪，含连续缺测。"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kfmu import KalmanFilter, LinearKalmanModel

dt = 1.0
F = np.array([[1, dt], [0, 1]])
H = np.array([[1.0, 0.0]])
q = 0.002
Q = q * np.array([[dt**4 / 4, dt**3 / 2], [dt**3 / 2, dt**2]])
R = np.array([[1.0]])

model = LinearKalmanModel(F=F, H=H, Q=Q, R=R)
kf = KalmanFilter(model, x0=np.zeros(2), P0=np.eye(2) * 10.0)

measurements = [1.1, 2.0, np.nan, np.nan, 4.2]  # NaN = 该步缺测
for k, z in enumerate(measurements):
    kf.predict()
    result = kf.update(np.array([z]))
    print(f"k={k} status={result.status:15s} "
          f"x={np.round(kf.x, 3).tolist()} eig_min="
          f"{np.linalg.eigvalsh(kf.P)[0]:.2e}")
