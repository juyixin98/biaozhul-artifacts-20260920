import json
import os
import subprocess
import sys

ENV = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
CLI = [sys.executable, "-m", "maskcompiler"]


def run(*args):
    return subprocess.run(
        CLI + list(args), capture_output=True, text=True, env=ENV, timeout=60
    )


def test_cli_keygen_compile_apply(tmp_path):
    key = str(tmp_path / "keys.json")
    rules = str(tmp_path / "rules.json")
    doc = str(tmp_path / "doc.json")

    proc = run("keygen", "--out", key)
    assert proc.returncode == 0, proc.stderr
    assert "0600" in proc.stdout

    with open(rules, "w", encoding="utf-8") as fh:
        json.dump(
            {"version": 1, "rules": [
                {"id": "phone", "path": "$.users[*].phone", "transform": "mask",
                 "params": {"keep_last": 4}, "priority": 1},
                {"id": "name", "path": "$..name", "transform": "redact", "priority": 2},
            ]},
            fh, ensure_ascii=False,
        )
    with open(doc, "w", encoding="utf-8") as fh:
        json.dump({"users": [{"phone": "13812345678", "name": "张三"}]},
                  fh, ensure_ascii=False)

    proc = run("compile", "-r", rules)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True

    proc = run("apply", "-r", rules, "-d", doc, "--key-file", key)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["output"]["users"][0]["phone"] == "*******5678"
    assert payload["output"]["users"][0]["name"] is None
    assert payload["report"]["matched_rules"] == {"phone": 1, "name": 1}


def test_cli_invalid_ruleset_exits_nonzero(tmp_path):
    bad = str(tmp_path / "bad.json")
    with open(bad, "w") as fh:
        json.dump({"version": 1, "rules": [
            {"id": "r", "path": "$.x", "transform": "unknown", "priority": 1}
        ]}, fh)
    proc = run("compile", "-r", bad)
    assert proc.returncode == 2
    assert "UnknownTransformError" in proc.stderr


def test_cli_missing_file_exit_code(tmp_path):
    proc = run("compile", "-r", str(tmp_path / "missing.json"))
    assert proc.returncode == 2
