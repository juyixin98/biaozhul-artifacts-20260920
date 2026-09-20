"""Path-traversal / symlink-escape protection and dataset loading rules."""
from __future__ import annotations

import os

import numpy as np
import pytest

from app import datasets as dsmod
from app.config import get_settings

from .conftest import WHITELIST


def test_traversal_dotdot_rejected(tmp_path):
    # tmp_path is OUTSIDE the whitelist; even an existing file is denied.
    evil = tmp_path / "secret.csv"
    evil.write_text("a,b\n1,2\n")
    with pytest.raises(dsmod.DatasetError, match="outside"):
        dsmod.safe_resolve(str(evil))


def test_absolute_path_outside_whitelist_rejected():
    with pytest.raises(dsmod.DatasetError, match="outside"):
        dsmod.safe_resolve("/etc/hostname")


def test_dotdot_inside_string_resolves_outside():
    # Whitelist/<anything>/../../etc/hostname escapes and must be rejected.
    candidate = os.path.join(str(WHITELIST), "a", "..", "..", "..", "etc", "hostname")
    with pytest.raises(dsmod.DatasetError):
        dsmod.safe_resolve(candidate)


def test_symlink_escape_rejected(tmp_under_whitelist, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("a,b\n1,2\n")
    link = tmp_under_whitelist("link.csv")
    os.symlink(str(secret), link)
    assert os.path.realpath(link) == str(secret)
    with pytest.raises(dsmod.DatasetError, match="outside"):
        dsmod.safe_resolve(link)


def test_symlink_within_whitelist_allowed(tmp_under_whitelist, csv_dataset):
    link = tmp_under_whitelist("inside_link.csv")
    os.symlink(csv_dataset, link)
    resolved = dsmod.safe_resolve(link)
    assert resolved == os.path.realpath(csv_dataset)


def test_missing_file_rejected():
    with pytest.raises(dsmod.DatasetError, match="not found"):
        dsmod.safe_resolve(os.path.join(str(WHITELIST), "does-not-exist.csv"))


def test_unsupported_extension(tmp_under_whitelist):
    p = tmp_under_whitelist("x.txt")
    with open(p, "w") as f:
        f.write("hello")
    with pytest.raises(dsmod.DatasetError, match="unsupported"):
        dsmod.detect_format(p)


def test_csv_label_column_selection(tmp_under_whitelist):
    import csv

    p = tmp_under_whitelist("lc.csv")
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["target", "f1", "f2"])
        w.writerow([0, 0.1, 0.2])
        w.writerow([1, 0.9, 0.8])
        w.writerow([1, 0.7, 0.6])
    info = dsmod.inspect_dataset(p, task="classification", label_column="target")
    assert info["num_features"] == 2
    assert info["num_rows"] == 3
    np.testing.assert_array_equal(info["y"], [0, 1, 1])


def test_csv_missing_label_column(tmp_under_whitelist):
    p = tmp_under_whitelist("nope.csv")
    with open(p, "w") as f:
        f.write("a,b\n1,2\n")
    with pytest.raises(dsmod.DatasetError, match="label column"):
        dsmod.inspect_dataset(p, label_column="zzz")


def test_csv_ragged_row_rejected(tmp_under_whitelist):
    p = tmp_under_whitelist("ragged.csv")
    with open(p, "w") as f:
        f.write("a,b,y\n1,2,0\n3,4\n")
    with pytest.raises(dsmod.DatasetError, match="fields"):
        dsmod.inspect_dataset(p)


def test_npy_wrong_ndim_rejected(tmp_under_whitelist):
    p = tmp_under_whitelist("d1.npy")
    np.save(p, np.array([1.0, 2.0, 3.0]))
    with pytest.raises(dsmod.DatasetError, match="2-D"):
        dsmod.inspect_dataset(p, task="regression")


def test_classification_non_integer_labels_rejected(tmp_under_whitelist):
    p = tmp_under_whitelist("frac.npy")
    np.save(p, np.array([[1.0, 0.5], [2.0, 0.7]], dtype=np.float32))
    with pytest.raises(dsmod.DatasetError, match="integers"):
        dsmod.inspect_dataset(p, task="classification")


def test_split_is_deterministic_and_disjoint():
    s1 = dsmod.make_split(100, 0.25, seed=123)
    s2 = dsmod.make_split(100, 0.25, seed=123)
    assert s1 == s2
    assert set(s1["train"]).isdisjoint(s1["val"])
    assert len(s1["train"]) + len(s1["val"]) == 100


def test_split_changes_with_seed():
    s1 = dsmod.make_split(100, 0.25, seed=1)
    s2 = dsmod.make_split(100, 0.25, seed=2)
    assert s1 != s2


def test_digest_mismatch_detected(csv_dataset):
    with pytest.raises(dsmod.DatasetError, match="digest mismatch"):
        dsmod.verify_digest(csv_dataset, "0" * 64)
