"""CSV boundary loader tests against on-disk fixtures and temp files."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from ipw_ate.errors import DataValidationError
from ipw_ate.io_csv import load_csv

FIX = str(Path(__file__).parent / "fixtures" / "good_overlap.csv")


def test_load_good_fixture():
    t, y, x, names = load_csv(FIX)
    assert t.ndim == 1 and x.ndim == 2
    assert set(np.unique(t).tolist()) <= {0.0, 1.0}
    assert names == ("x0", "x1")
    assert x.shape[0] == t.shape[0] == y.shape[0]
    assert 0 < t.mean() < 1


def test_missing_required_column(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("a,b\n1,2\n3,4\n")
    with pytest.raises(DataValidationError):
        load_csv(str(p))


def test_no_covariate_columns(tmp_path):
    p = tmp_path / "nox.csv"
    p.write_text("t,y\n1,2\n0,4\n")
    with pytest.raises(DataValidationError):
        load_csv(str(p))


def test_wrong_field_count(tmp_path):
    p = tmp_path / "wrong.csv"
    p.write_text("t,y,x\n1,2\n0,4,5\n")
    with pytest.raises(DataValidationError):
        load_csv(str(p))


def test_non_numeric(tmp_path):
    p = tmp_path / "nn.csv"
    p.write_text("t,y,x\n1,oops,2\n0,4,5\n")
    with pytest.raises(DataValidationError):
        load_csv(str(p))


def test_empty_file(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("")
    with pytest.raises(DataValidationError):
        load_csv(str(p))
