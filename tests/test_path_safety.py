"""Path traversal and symlink-escape protection."""
from __future__ import annotations

import os

import pytest

from app.services import data_safe
from app.services.data_safe import PathViolation, resolve_within_whitelist


def test_relative_path_inside_root(whitelist, sample_data):
    p = resolve_within_whitelist("samples.csv")
    assert p == sample_data["csv"]


def test_dotdot_traversal_rejected(whitelist):
    with pytest.raises(PathViolation):
        resolve_within_whitelist(whitelist / ".." / ".." / "etc" / "passwd")


def test_absolute_outside_root_rejected(whitelist):
    with pytest.raises(PathViolation):
        resolve_within_whitelist("/etc/passwd")


def test_symlink_escape_rejected(whitelist):
    secret_dir = whitelist.parent / "secrets"
    secret_dir.mkdir(exist_ok=True)
    secret = secret_dir / "passwords.csv"
    secret.write_text("1,2,3\n")
    link = whitelist / "evil.csv"
    try:
        os.symlink(secret, link)
    except OSError:
        pytest.skip("symlinks not permitted in this filesystem")
    with pytest.raises(PathViolation):
        resolve_within_whitelist(link)


def test_symlink_to_outside_tree_even_with_csv_name(whitelist):
    target = whitelist.parent / "outside.csv"
    target.write_text("1,2\n")
    link = whitelist / "link.csv"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlinks not permitted")
    with pytest.raises(PathViolation):
        data_safe.load_dataset(str(link))


def test_internal_symlink_is_fine(whitelist, sample_data):
    # A symlink that stays within the whitelist resolves to a valid file.
    inner = whitelist / "inner.csv"
    os.symlink(sample_data["csv"], inner)
    p = resolve_within_whitelist(inner)
    assert p.is_file()


def test_missing_file_rejected(whitelist):
    with pytest.raises(PathViolation, match="does not exist"):
        resolve_within_whitelist(whitelist / "nope.csv")


def test_directory_not_a_file(whitelist):
    with pytest.raises(PathViolation, match="regular file"):
        resolve_within_whitelist(whitelist)


def test_load_csv_and_npy(sample_data):
    X, y = data_safe.load_dataset(str(sample_data["csv"]))
    assert X.shape == (90, 2)
    assert y.shape == (90,)
    X2, y2 = data_safe.load_dataset(
        str(sample_data["npy_x"]), str(sample_data["npy_y"]))
    assert X2.shape == (90, 2)
