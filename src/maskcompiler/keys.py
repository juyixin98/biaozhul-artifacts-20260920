"""Local key management.

No production accounts, no hard-coded secrets. Keys are generated locally with
``cryptography`` primitives and stored in an explicit, versioned JSON file
whose permissions are forced to 0600.

Key file format::

    {
      "version": 1,
      "hmac_key": "<urlsafe-base64 of >=32 random bytes>",
      "fernet_key": "<Fernet key, urlsafe-base64>"
    }

HMAC uses raw random symmetric keys; reversible encryption uses Fernet
(authenticated symmetric encryption built on AES-128-CBC + HMAC-SHA256).
"""

import base64
import json
import os
import stat
from dataclasses import dataclass
from typing import Optional

from cryptography.fernet import Fernet

from .errors import KeyManagerError

KEY_FILE_VERSION = 1
MIN_HMAC_KEY_BYTES = 32


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


@dataclass(frozen=True)
class KeyBundle:
    hmac_key: bytes
    fernet_key: bytes

    def fernet(self) -> Fernet:
        return Fernet(self.fernet_key)


def generate_bundle(hmac_key_bytes: int = MIN_HMAC_KEY_BYTES) -> KeyBundle:
    """Generate a fresh random bundle (uses ``os.urandom`` via cryptography)."""
    if hmac_key_bytes < MIN_HMAC_KEY_BYTES:
        raise KeyManagerError("HMAC keys must be at least %d bytes" % MIN_HMAC_KEY_BYTES)
    return KeyBundle(
        hmac_key=os.urandom(hmac_key_bytes),
        fernet_key=Fernet.generate_key(),
    )


def bundle_to_dict(bundle: KeyBundle) -> dict:
    return {
        "version": KEY_FILE_VERSION,
        "hmac_key": _b64e(bundle.hmac_key),
        "fernet_key": bundle.fernet_key.decode("ascii"),
    }


def bundle_from_dict(data: dict) -> KeyBundle:
    if not isinstance(data, dict) or data.get("version") != KEY_FILE_VERSION:
        raise KeyManagerError("unsupported key file (need version=%d)" % KEY_FILE_VERSION)
    try:
        hmac_key = _b64d(data["hmac_key"])
        fernet_key = data["fernet_key"].encode("ascii")
    except (KeyError, TypeError, ValueError) as exc:
        raise KeyManagerError("malformed key file: missing or undecodable key material") from exc
    if len(hmac_key) < MIN_HMAC_KEY_BYTES:
        raise KeyManagerError("HMAC key too short (need >= %d bytes)" % MIN_HMAC_KEY_BYTES)
    try:
        Fernet(fernet_key)
    except (ValueError, TypeError) as exc:
        raise KeyManagerError("malformed Fernet key") from exc
    return KeyBundle(hmac_key=hmac_key, fernet_key=fernet_key)


def save_keyfile(path: str, bundle: Optional[KeyBundle] = None) -> KeyBundle:
    """Create ``path`` with a fresh bundle, chmod 0600. Refuse to overwrite."""
    if os.path.exists(path):
        raise KeyManagerError("refusing to overwrite existing key file: %s" % path)
    bundle = bundle or generate_bundle()
    # Create with restrictive permissions from the start (O_EXCL guards races).
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(bundle_to_dict(bundle), fh, indent=2)
            fh.write("\n")
    except Exception:
        _try_unlink(path)
        raise
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return bundle


def load_keyfile(path: str) -> KeyBundle:
    if not os.path.exists(path):
        raise KeyManagerError("key file not found: %s" % path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise KeyManagerError(
            "refusing to use key file accessible to group/others (mode %04o); chmod 600 %s"
            % (mode, path)
        )
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise KeyManagerError("key file is not valid JSON") from exc
    return bundle_from_dict(data)


def _try_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
