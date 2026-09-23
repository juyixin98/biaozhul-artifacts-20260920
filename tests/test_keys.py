import json
import os
import stat

import pytest

from maskcompiler.keys import (
    KEY_FILE_VERSION,
    generate_bundle,
    load_keyfile,
    save_keyfile,
)
from maskcompiler.errors import KeyManagerError


def test_keygen_creates_0600_file(key_file):
    mode = stat.S_IMODE(os.stat(key_file).st_mode)
    assert mode == 0o600
    with open(key_file, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["version"] == KEY_FILE_VERSION
    assert len(data["hmac_key"]) > 40
    assert data["fernet_key"]


def test_keyfile_roundtrip(key_file):
    bundle = load_keyfile(key_file)
    assert len(bundle.hmac_key) >= 32
    bundle.fernet()  # valid fernet key


def test_refuses_overwrite(key_file):
    with pytest.raises(KeyManagerError):
        save_keyfile(key_file)


def test_group_or_world_readable_keyfile_rejected(key_file):
    os.chmod(key_file, 0o644)
    with pytest.raises(KeyManagerError):
        load_keyfile(key_file)


def test_bad_version_rejected(tmp_path):
    path = str(tmp_path / "k.json")
    bundle = generate_bundle()
    data = {
        "version": 999,
        "hmac_key": bundle_to_b64(bundle),
        "fernet_key": bundle.fernet_key.decode(),
    }
    with open(path, "w") as fh:
        json.dump(data, fh)
    with pytest.raises(KeyManagerError):
        load_keyfile(path)


def test_short_hmac_key_rejected(tmp_path):
    from cryptography.fernet import Fernet
    import base64

    data = {
        "version": 1,
        "hmac_key": base64.urlsafe_b64encode(b"short").decode(),
        "fernet_key": Fernet.generate_key().decode(),
    }
    path = str(tmp_path / "k2.json")
    with open(path, "w") as fh:
        json.dump(data, fh)
    os.chmod(path, 0o600)
    with pytest.raises(KeyManagerError):
        load_keyfile(path)


def bundle_to_b64(bundle):
    import base64

    return base64.urlsafe_b64encode(bundle.hmac_key).decode()
