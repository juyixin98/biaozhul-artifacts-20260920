import json
from pathlib import Path

import pytest

from p48_gateway.registry import Registry, read_persisted_epoch, write_persisted_epoch

VALID = {
    "admin_key": "admin-key-0123456789abcdef",
    "default_ttl_seconds": 3.0,
    "epoch_key": "ioksSseGEHLK3_AT1mQIr2bI5w2gAv-EwTSdm2g7ZPY",
    "testers": {
        "tester_alpha": {
            "key": "6SkRyx5Fb07IwBAyLkR0y1pSajsbWoG0avrbibiQNP4",
            "robots": ["alpha", "bravo"],
        },
        "tester_bravo": {
            "key": "-jy51chNDGv8X56cp3X7xYKqXz6LMpD1rdr9p1ECn8A",
            "robots": ["bravo"],
        },
    },
    "robots": {
        "alpha": {"namespace": "/p48/alpha"},
        "bravo": {"namespace": "/p48/bravo"},
    },
}


def test_valid_registry_loads():
    reg = Registry.from_data(VALID)
    assert reg.default_ttl == 3.0
    assert reg.tester_can_access(reg.testers["tester_alpha"], "alpha")
    assert reg.tester_can_access(reg.testers["tester_alpha"], "bravo")
    assert not reg.tester_can_access(reg.testers["tester_bravo"], "alpha")
    assert reg.tester_can_access(reg.testers["tester_bravo"], "bravo")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["robots"]["alpha"].__setitem__("namespace", "p48/alpha"),
        lambda d: d["robots"]["alpha"].__setitem__("namespace", "/p48/../x"),
        lambda d: d["robots"].__setitem__(
            "bravo", {"namespace": "/p48/alpha/cmd"}),
        lambda d: d["robots"].__setitem__("a/b", {"namespace": "/p48/x"}),
        lambda d: d["testers"]["tester_alpha"].__setitem__(
            "robots", ["ghost"]),
        lambda d: d["testers"]["tester_alpha"].__setitem__(
            "key", "not-a-valid-key"),
        lambda d: d.__setitem__("admin_key", "short"),
    ],
)
def test_invalid_registries_rejected(mutate):
    data = json.loads(json.dumps(VALID))
    mutate(data)
    with pytest.raises(Exception):
        Registry.from_data(data)


def test_duplicate_namespace_rejected():
    data = json.loads(json.dumps(VALID))
    data["robots"]["alpha"]["namespace"] = "/p48/bravo"
    with pytest.raises(ValueError, match="twice"):
        Registry.from_data(data)


def test_nested_namespace_rejected():
    data = json.loads(json.dumps(VALID))
    data["robots"]["bravo"]["namespace"] = "/p48/alpha/cmd"
    with pytest.raises(ValueError, match="nests"):
        Registry.from_data(data)
    # reverse ordering
    data = json.loads(json.dumps(VALID))
    data["robots"] = {
        "bravo": {"namespace": "/p48/alpha/cmd"},
        "alpha": {"namespace": "/p48/alpha"},
    }
    with pytest.raises(ValueError, match="nests"):
        Registry.from_data(data)


def test_epoch_sidecar_monotonic(tmp_path: Path):
    path = tmp_path / "reg.json"
    path.write_text(json.dumps(VALID))
    assert read_persisted_epoch(path) == 1
    write_persisted_epoch(path, 7)
    assert read_persisted_epoch(path) == 7
