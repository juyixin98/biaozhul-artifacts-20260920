import sys
from pathlib import Path

import numpy as np
import scipy.sparse as sp

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = ROOT / "fixtures"


def load_fixture(name):
    """Load a synthetic fixture as (csr_matrix, vector)."""
    data = np.load(FIXTURES / f"{name}.npz")
    shape = tuple(int(x) for x in data["shape"])
    matrix = sp.coo_matrix(
        (data["data"], (data["row"], data["col"])), shape=shape
    ).tocsr()
    return matrix, np.asarray(data["vector"], dtype=np.float64)
