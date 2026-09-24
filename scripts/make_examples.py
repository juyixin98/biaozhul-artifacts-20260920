#!/usr/bin/env python3
"""Generate example input bundles under examples/.

Every tarball written into ``vendor/`` is a real gzip-compressed tar whose
sha512 digest is computed here and embedded into the lockfile, so the
gate's content verification runs against genuine bytes.

Scenarios produced:
  01-clean-lock          fully pinned + artifact-verified, reproducible
  02-lock-drift          package.json range excludes the locked version
  03-duplicate-versions  same dependency locked at two versions
  04-missing-integrity   one lock node lacks an integrity digest
  05-platform-optional   windows-only optional dep, audit on linux/x64
  06-cycle               a -> b -> a dependency cycle
  07-unsupported-syntax  git+https specifier rejected
  08-workspaces          npm workspaces with a link entry
  09-tampered-artifact   on-disk tarball bytes disagree with the digest
  10-lock-only-no-artifacts  perfectly pinned lock, zero tarballs: present
                         lockfile is still NOT reproducible (thesis case)
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "examples")


def tarball_bytes(name: str, version: str, *, payload: bytes | None = None,
                  install_script: bool = False) -> bytes:
    """Build an in-memory npm-style tarball: package/package.json + file."""
    pkg_json = {
        "name": name,
        "version": version,
    }
    if install_script:
        pkg_json["scripts"] = {"install": "node-gyp rebuild || true"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        _add(tf, "package/package.json", json.dumps(pkg_json, indent=2).encode())
        _add(tf, "package/index.js", payload or f"module.exports = {json.dumps(name)};\n".encode())
    return buf.getvalue()


def _add(tf: tarfile.TarFile, path: str, data: bytes) -> None:
    info = tarfile.TarInfo(path)
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))


def sri(data: bytes, alg: str = "sha512") -> str:
    return f"{alg}-{base64.b64encode(hashlib.new(alg, data).digest()).decode()}"


def make_bundle(path: str, files: dict[str, bytes]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def node(name: str, version: str, blob: bytes, deps=None, optional=None,
         **extra) -> tuple[dict, bytes]:
    entry = {
        "version": version,
        "resolved": f"https://registry.npmjs.org/{name}/-/{name.split('/')[-1]}-{version}.tgz",
        "integrity": sri(blob),
    }
    if deps:
        entry["dependencies"] = deps
    entry.update(extra)
    if optional is not None:
        entry["optional"] = bool(optional)
    return entry, blob


def build():
    # ---- shared artifacts ------------------------------------------------
    left_pad_1_2_3 = tarball_bytes("left-pad", "1.2.3")
    left_pad_1_2_9 = tarball_bytes("left-pad", "1.2.9", payload=b"module.exports='newer';\n")
    debug_4_3_4 = tarball_bytes("debug", "4.3.4")
    ms_2_1_3 = tarball_bytes("ms", "2.1.3")
    ms_3_0_0 = tarball_bytes("ms", "3.0.0")
    win_drv = tarball_bytes("win-native", "1.0.0", install_script=True)
    cyc_a = tarball_bytes("cyclic-a", "1.0.0")
    cyc_b = tarball_bytes("cyclic-b", "1.0.0")
    lodash = tarball_bytes("lodash", "4.17.21")
    tool = tarball_bytes("@scope/tool", "2.0.0")

    # ---- 01 clean --------------------------------------------------------
    pkg = {"name": "demo-app", "version": "1.0.0",
           "dependencies": {"left-pad": "^1.2.0", "debug": "^4.3.0"},
           "devDependencies": {"lodash": "^4.17.0"}}
    lock = {
        "name": "demo-app", "version": "1.0.0", "lockfileVersion": 3, "requires": True,
        "packages": {
            "": {"name": "demo-app", "version": "1.0.0",
                 "dependencies": {"left-pad": "^1.2.0", "debug": "^4.3.0"},
                 "devDependencies": {"lodash": "^4.17.0"}},
            "node_modules/left-pad": node("left-pad", "1.2.3", left_pad_1_2_3)[0],
            "node_modules/lodash": node("lodash", "4.17.21", lodash, dev=True)[0],
            "node_modules/debug": node("debug", "4.3.4", debug_4_3_4,
                                       deps={"ms": "2.1.3"})[0],
            "node_modules/ms": node("ms", "2.1.3", ms_2_1_3)[0],
        },
    }
    make_bundle(f"{EX}/01-clean-lock/bundle.tar.gz", {
        "package.json": _j(pkg),
        "package-lock.json": _j(lock),
        "vendor/left-pad-1.2.3.tgz": left_pad_1_2_3,
        "vendor/lodash-4.17.21.tgz": lodash,
        "vendor/debug-4.3.4.tgz": debug_4_3_4,
        "vendor/ms-2.1.3.tgz": ms_2_1_3,
    })

    # ---- 02 drift: declared ^1.0.0 but lock has 2.0.0-ish ---------------
    pkg2 = {"name": "drifty", "version": "0.1.0",
            "dependencies": {"left-pad": "~1.2.0"}}
    lock2 = {
        "name": "drifty", "version": "0.1.0", "lockfileVersion": 3,
        "packages": {
            "": {"name": "drifty", "version": "0.1.0",
                 "dependencies": {"left-pad": "~1.2.0"}},
            "node_modules/left-pad": node("left-pad", "1.3.0",
                                          tarball_bytes("left-pad", "1.3.0"))[0],
        },
    }
    make_bundle(f"{EX}/02-lock-drift/bundle.tar.gz", {
        "package.json": _j(pkg2),
        "package-lock.json": _j(lock2),
        "vendor/left-pad-1.3.0.tgz": tarball_bytes("left-pad", "1.3.0"),
    })

    # ---- 03 duplicate versions of ms ------------------------------------
    pkg3 = {"name": "dupy", "version": "1.0.0", "dependencies": {"debug": "^4.3.0",
                                                                 "@scope/tool": "^2.0.0"}}
    lock3 = {
        "name": "dupy", "version": "1.0.0", "lockfileVersion": 3,
        "packages": {
            "": {"name": "dupy", "version": "1.0.0",
                 "dependencies": {"debug": "^4.3.0", "@scope/tool": "^2.0.0"}},
            "node_modules/debug": node("debug", "4.3.4", debug_4_3_4,
                                       deps={"ms": "2.1.3"})[0],
            "node_modules/ms": node("ms", "2.1.3", ms_2_1_3)[0],
            "node_modules/@scope/tool": node("@scope/tool", "2.0.0", tool,
                                             deps={"ms": "3.0.0"})[0],
            "node_modules/@scope/tool/node_modules/ms": node("ms", "3.0.0", ms_3_0_0)[0],
        },
    }
    make_bundle(f"{EX}/03-duplicate-versions/bundle.tar.gz", {
        "package.json": _j(pkg3),
        "package-lock.json": _j(lock3),
        "vendor/debug-4.3.4.tgz": debug_4_3_4,
        "vendor/ms-2.1.3.tgz": ms_2_1_3,
        "vendor/tool-2.0.0.tgz": tool,
        "vendor/ms-3.0.0.tgz": ms_3_0_0,
    })

    # ---- 04 missing integrity -------------------------------------------
    bad = node("left-pad", "1.2.3", left_pad_1_2_3)[0]
    del bad["integrity"]
    pkg4 = {"name": "nohash", "version": "1.0.0", "dependencies": {"left-pad": "^1.2.0"}}
    lock4 = {"name": "nohash", "version": "1.0.0", "lockfileVersion": 3,
             "packages": {"": {"name": "nohash", "version": "1.0.0",
                               "dependencies": {"left-pad": "^1.2.0"}},
                          "node_modules/left-pad": bad}}
    make_bundle(f"{EX}/04-missing-integrity/bundle.tar.gz", {
        "package.json": _j(pkg4),
        "package-lock.json": _j(lock4),
        "vendor/left-pad-1.2.3.tgz": left_pad_1_2_3,
    })

    # ---- 05 platform optional -------------------------------------------
    pkg5 = {"name": "plat", "version": "1.0.0",
            "optionalDependencies": {"win-native": "^1.0.0"}}
    lock5 = {"name": "plat", "version": "1.0.0", "lockfileVersion": 3,
             "packages": {
                 "": {"name": "plat", "version": "1.0.0",
                      "optionalDependencies": {"win-native": "^1.0.0"}},
                 "node_modules/win-native": node(
                     "win-native", "1.0.0", win_drv, optional=True,
                     os=["win32"], cpu=["x64"], hasInstallScript=True)[0],
             }}
    # npm's real key is hasInstallScript; inject script on the tarball side
    make_bundle(f"{EX}/05-platform-optional/bundle.tar.gz", {
        "package.json": _j(pkg5),
        "package-lock.json": _j(lock5),
        "vendor/win-native-1.0.0.tgz": win_drv,
    })

    # ---- 06 cycle --------------------------------------------------------
    pkg6 = {"name": "loopy", "version": "1.0.0", "dependencies": {"cyclic-a": "^1.0.0"}}
    lock6 = {"name": "loopy", "version": "1.0.0", "lockfileVersion": 3,
             "packages": {
                 "": {"name": "loopy", "version": "1.0.0",
                      "dependencies": {"cyclic-a": "^1.0.0"}},
                 "node_modules/cyclic-a": node("cyclic-a", "1.0.0", cyc_a,
                                               deps={"cyclic-b": "^1.0.0"})[0],
                 "node_modules/cyclic-b": node("cyclic-b", "1.0.0", cyc_b,
                                               deps={"cyclic-a": "^1.0.0"})[0],
             }}
    make_bundle(f"{EX}/06-cycle/bundle.tar.gz", {
        "package.json": _j(pkg6),
        "package-lock.json": _j(lock6),
        "vendor/cyclic-a-1.0.0.tgz": cyc_a,
        "vendor/cyclic-b-1.0.0.tgz": cyc_b,
    })

    # ---- 07 unsupported syntax ------------------------------------------
    pkg7 = {"name": "wild", "version": "1.0.0",
            "dependencies": {"left-pad": "git+https://github.com/azer/left-pad.git#abc1234"}}
    lock7 = {"name": "wild", "version": "1.0.0", "lockfileVersion": 3,
             "packages": {
                 "": {"name": "wild", "version": "1.0.0",
                      "dependencies": {"left-pad": "git+https://github.com/azer/left-pad.git#abc1234"}},
                 "node_modules/left-pad": {
                     "version": "1.2.3",
                     "resolved": "git+https://github.com/azer/left-pad.git#abc1234",
                 },
             }}
    make_bundle(f"{EX}/07-unsupported-syntax/bundle.tar.gz", {
        "package.json": _j(pkg7),
        "package-lock.json": _j(lock7),
    })

    # ---- 08 workspaces ---------------------------------------------------
    ws_pkg = {"name": "monorepo", "version": "0.0.0", "private": True,
              "workspaces": ["packages/*"],
              "dependencies": {"left-pad": "^1.2.0"}}
    ws_lib = {"name": "@mono/lib", "version": "1.4.0",
              "dependencies": {"ms": "^2.1.0"}}
    ws_app = {"name": "@mono/app", "version": "0.2.0",
              "dependencies": {"@mono/lib": "^1.4.0"}}
    lock8 = {"name": "monorepo", "version": "0.0.0", "lockfileVersion": 3,
             "packages": {
                 "": {"name": "monorepo", "version": "0.0.0",
                      "workspaces": ["packages/*"],
                      "dependencies": {"left-pad": "^1.2.0"}},
                 "node_modules/left-pad": node("left-pad", "1.2.3", left_pad_1_2_3)[0],
                 "node_modules/ms": node("ms", "2.1.3", ms_2_1_3)[0],
                 "node_modules/@mono/lib": {
                     "resolved": "../../packages/lib", "link": True,
                     "version": "1.4.0",
                     "dependencies": {"ms": "^2.1.0"},
                 },
                 "node_modules/@mono/app": {
                     "resolved": "../../packages/app", "link": True,
                     "version": "0.2.0",
                     "dependencies": {"@mono/lib": "^1.4.0"},
                 },
                 "packages/lib": {"name": "@mono/lib", "version": "1.4.0",
                                  "dependencies": {"ms": "^2.1.0"}},
                 "packages/app": {"name": "@mono/app", "version": "0.2.0",
                                  "dependencies": {"@mono/lib": "^1.4.0"}},
             }}
    make_bundle(f"{EX}/08-workspaces/bundle.tar.gz", {
        "package.json": _j(ws_pkg),
        "package-lock.json": _j(lock8),
        "packages/lib/package.json": _j(ws_lib),
        "packages/app/package.json": _j(ws_app),
        "vendor/left-pad-1.2.3.tgz": left_pad_1_2_3,
        "vendor/ms-2.1.3.tgz": ms_2_1_3,
    })

    # ---- 09 tampered artifact -------------------------------------------
    pkg9 = {"name": "tamper", "version": "1.0.0", "dependencies": {"left-pad": "^1.2.0"}}
    lock9 = {"name": "tamper", "version": "1.0.0", "lockfileVersion": 3,
             "packages": {
                 "": {"name": "tamper", "version": "1.0.0",
                      "dependencies": {"left-pad": "^1.2.0"}},
                 "node_modules/left-pad": node("left-pad", "1.2.3", left_pad_1_2_3)[0],
             }}
    # ship bytes whose real digest differs from the declared one: build a
    # genuinely different tarball (different payload) but keep the same path
    tampered = tarball_bytes("left-pad", "1.2.3", payload=b"module.exports='pwned';\n")
    make_bundle(f"{EX}/09-tampered-artifact/bundle.tar.gz", {
        "package.json": _j(pkg9),
        "package-lock.json": _j(lock9),
        "vendor/left-pad-1.2.3.tgz": tampered,
    })

    # ---- 10 lock-only: perfectly pinned, but zero artifacts --------------
    pkg10 = {"name": "lockonly", "version": "1.0.0", "dependencies": {"left-pad": "^1.2.0"}}
    lock10 = {"name": "lockonly", "version": "1.0.0", "lockfileVersion": 3,
              "packages": {
                  "": {"name": "lockonly", "version": "1.0.0",
                       "dependencies": {"left-pad": "^1.2.0"}},
                  "node_modules/left-pad": node("left-pad", "1.2.3", left_pad_1_2_3)[0],
              }}
    make_bundle(f"{EX}/10-lock-only-no-artifacts/bundle.tar.gz", {
        "package.json": _j(pkg10),
        "package-lock.json": _j(lock10),
    })

    print("examples written under", EX)


def _j(obj) -> bytes:
    return (json.dumps(obj, indent=2) + "\n").encode()


if __name__ == "__main__":
    build()
