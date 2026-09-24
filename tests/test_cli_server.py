"""CLI 与 HTTP 服务测试。"""

import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

from urdf_check.cli import main as cli_main
from urdf_check.server import CheckHandler

from .conftest import inertial_xml, link_xml, make_urdf, read_example


# ---------- CLI ----------

def test_cli_good_file_exit_0(tmp_path, capsys):
    f = tmp_path / "ok.urdf"
    f.write_bytes(read_example("two_link_arm.urdf"))
    assert cli_main([str(f)]) == 0
    out = capsys.readouterr().out
    assert "通过" in out


def test_cli_bad_file_exit_1(tmp_path):
    f = tmp_path / "bad.urdf"
    f.write_bytes(make_urdf(link_xml("a", inertial_xml(mass="0.0"))))
    assert cli_main([str(f)]) == 1


def test_cli_unsafe_file_exit_2(tmp_path):
    f = tmp_path / "xxe.urdf"
    f.write_bytes(
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE robot [ <!ENTITY x SYSTEM "file:///etc/passwd"> ]>\n'
        b'<robot name="x"><link name="a"/></robot>\n'
    )
    assert cli_main([str(f)]) == 2


def test_cli_json_output(tmp_path, capsys):
    f = tmp_path / "ok.urdf"
    f.write_bytes(read_example("fixed_sensor_rig.urdf"))
    assert cli_main([str(f), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["robot_name"] == "fixed_sensor_rig"
    assert payload["link_count"] == 2


def test_cli_missing_file_exit_2(tmp_path):
    assert cli_main([str(tmp_path / "nope.urdf")]) == 2


# ---------- HTTP 服务 ----------

@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), CheckHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address
    httpd.shutdown()
    httpd.server_close()


def _post(port, body: bytes):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/check", data=body, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_server_healthz(server):
    _, port = server
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/healthz", timeout=5
    ) as resp:
        assert resp.status == 200
        assert json.loads(resp.read())["status"] == "ok"


def test_server_check_ok(server):
    _, port = server
    status, payload = _post(port, read_example("two_link_arm.urdf"))
    assert status == 200
    assert payload["ok"] is True


def test_server_check_bad_urdf_422(server):
    _, port = server
    body = make_urdf(link_xml("a", inertial_xml(mass="0.0")))
    status, payload = _post(port, body)
    assert status == 422
    assert payload["ok"] is False
    assert any(d["code"] == "MASS_ZERO" for d in payload["diagnostics"])


def test_server_rejects_xxe(server):
    _, port = server
    body = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE robot [ <!ENTITY x SYSTEM "file:///etc/passwd"> ]>\n'
        b'<robot name="x"><link name="a"/></robot>\n'
    )
    status, payload = _post(port, body)
    assert status == 422
    assert payload["diagnostics"][0]["code"] in (
        "DTD_FORBIDDEN", "ENTITY_FORBIDDEN",
    )
