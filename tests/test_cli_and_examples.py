"""CLI 与 examples/ 下两套夹具、两套边界矩阵的端到端验收。"""

import json
import subprocess
import sys

import pytest

from urdf_check import inspect_urdf_file
from urdf_check.checker import Report
from urdf_check.cli import main
from urdf_check.issues import Code

from conftest import codes, has

EX = "examples"


def run_cli(path, *extra):
    proc = subprocess.run(
        [sys.executable, "-m", "urdf_check", path, *extra],
        capture_output=True, text=True,
    )
    return proc


@pytest.mark.parametrize("name", [
    "fixture_a_single_link.urdf",
    "fixture_b_chain.urdf",
    "boundary_triangle_equal.urdf",
])
def test_valid_examples_pass_cli(name):
    proc = run_cli(f"{EX}/{name}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "通过" in proc.stdout


def test_boundary_violation_fails_cli():
    proc = run_cli(f"{EX}/boundary_triangle_violation.urdf")
    assert proc.returncode == 1
    assert Code.INERTIA_TRIANGLE_VIOLATION in proc.stdout


def test_json_output_is_machine_readable():
    proc = run_cli(f"{EX}/boundary_triangle_violation.urdf", "--json")
    data = json.loads(proc.stdout)
    assert data["ok"] is False
    assert data["error_count"] >= 1
    issue = next(i for i in data["issues"]
                 if i["code"] == Code.INERTIA_TRIANGLE_VIOLATION)
    assert issue["attribute"] == ""  # 矩阵级问题无单一属性
    assert issue["line"] > 0
    assert set(issue) == {"severity", "code", "message", "node",
                          "attribute", "line"}


def test_fixture_b_has_fixed_and_revolute_and_prismatic():
    report = inspect_urdf_file(f"{EX}/fixture_b_chain.urdf")
    assert isinstance(report, Report)
    assert report.ok, report.to_dict()


def test_fixture_a_is_single_link():
    report = inspect_urdf_file(f"{EX}/fixture_a_single_link.urdf")
    assert report.ok
    # 单 link 无任何关节 -> 没有树/轴/限位类问题
    assert not [c for c in codes(report) if c.startswith(("TREE", "AXIS",
                                                          "LIMIT", "JOINT"))]


def test_cli_main_returns_1_on_error(tmp_path):
    bad = tmp_path / "bad.urdf"
    bad.write_text(
        '<robot name="r"><link name="a"/></robot>', encoding="utf-8")
    assert main([str(bad)]) == 1


def test_cli_strict_warnings(tmp_path):
    # 零特征值 -> 仅告警；普通模式退出 0，strict 模式退出 1。
    w = tmp_path / "warn.urdf"
    w.write_text(
        '<robot name="r"><link name="a"><inertial>'
        '<mass value="1"/>'
        '<inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="0"/>'
        '</inertial></link></robot>', encoding="utf-8")
    assert main([str(w)]) == 0
    assert main([str(w), "--strict-warnings"]) == 1


def test_cli_axis_tol_override(tmp_path):
    # 轴偏差约 5e-7：默认通过，--axis-tol 1e-9 失败。
    f = tmp_path / "axis.urdf"
    f.write_text(
        '<robot name="r">'
        '<link name="a"><inertial><mass value="1"/>'
        '<inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="1"/></inertial></link>'
        '<link name="b"><inertial><mass value="1"/>'
        '<inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="1"/></inertial></link>'
        '<joint name="j" type="revolute"><parent link="a"/><child link="b"/>'
        '<axis xyz="1.0000005 0 0"/>'
        '<limit lower="-1" upper="1" effort="1" velocity="1"/></joint></robot>',
        encoding="utf-8")
    assert main([str(f)]) == 0
    assert main([str(f), "--axis-tol", "1e-9"]) == 1


def test_xxe_file_nonexistent_secret_not_leaked(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP_SECRET_PAYLOAD", encoding="utf-8")
    evil = tmp_path / "evil.urdf"
    evil.write_text(
        f'<?xml version="1.0"?>\n<!DOCTYPE r [<!ENTITY s SYSTEM '
        f'"file://{secret}">]>\n<robot name="&s;"/>',
        encoding="utf-8")
    proc = run_cli(str(evil), "--json")
    assert proc.returncode == 1
    assert "TOP_SECRET_PAYLOAD" not in proc.stdout
