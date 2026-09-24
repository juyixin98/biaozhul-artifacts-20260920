"""End-to-end verifier tests including the five mandated scenarios:

* lock drift (range/spec drift and lockfile drift),
* duplicate versions (redundant and conflicting),
* missing integrity,
* platform-specific optional dependencies,
* circular dependencies.

Additional cases: unsupported syntax, workspace modelling, vendored hash
verification and the "lock file != reproducibility" guarantee.
"""

from __future__ import annotations

import json

import pytest

from tests._fixtures import make_bundle, make_tarball, manifest
from app.verifier import verify_bundle


def _happy(builders):
    """left-pad@1.3.0 and ms@2.1.3, both vendored and hash-verified."""
    b = builders
    leftpad = b["make_tarball"]("left-pad", "1.3.0")
    ms = b["make_tarball"]("ms", "2.1.3")
    lp_key, lp_node, lp_vendor = b["node"]("left-pad", "1.3.0", tarball=leftpad)
    ms_key, ms_node, ms_vendor = b["node"]("ms", "2.1.3", tarball=ms)
    lock = b["lockfile3"](
        {
            lp_key: lp_node,
            ms_key: ms_node,
        },
        root_extra={"dependencies": {"left-pad": "^1.3.0", "ms": "^2.1.0"}},
    )
    pkg = manifest(deps={"left-pad": "^1.3.0", "ms": "^2.1.0"})
    return b["build_files"](pkg, lock, [lp_vendor, ms_vendor])


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_bundle_passes_and_is_reproducible(builders):
    report = verify_bundle(_happy(builders))
    assert report["status"] == "pass", json.dumps(report, indent=2)
    assert report["reproducibility"]["status"] is True
    assert report["summary"]["vendored_tarballs_verified"] == 2
    assert report["summary"]["errors"] == 0


def test_lockfile_alone_is_not_reproducible(builders):
    b = builders
    leftpad = b["make_tarball"]("left-pad", "1.3.0")
    key, node_data, _vendor = b["node"]("left-pad", "1.3.0", tarball=leftpad)
    lock = b["lockfile3"](
        {key: node_data},
        root_extra={"dependencies": {"left-pad": "^1.3.0"}},
    )
    pkg = manifest(deps={"left-pad": "^1.3.0"})
    files = b["build_files"](pkg, lock, [])
    report = verify_bundle(files)
    # Gate may pass (the declared graph is consistent) but reproducibility MUST
    # be false — a lock file is not, by itself, proof of reproducibility.
    assert report["status"] == "pass"
    assert report["reproducibility"]["status"] is False
    assert any(
        "missing vendored artifact" in r for r in report["reproducibility"]["reasons"]
    )


def test_require_vendored_makes_missing_tarball_an_error(builders):
    b = builders
    leftpad = b["make_tarball"]("left-pad", "1.3.0")
    key, node_data, _vendor = b["node"]("left-pad", "1.3.0", tarball=leftpad)
    lock = b["lockfile3"](
        {key: node_data},
        root_extra={"dependencies": {"left-pad": "^1.3.0"}},
    )
    report = verify_bundle(
        b["build_files"](manifest(deps={"left-pad": "^1.3.0"}), lock, []),
        require_vendored_tarballs=True,
    )
    assert report["status"] == "fail"
    codes = {f["code"] for f in report["findings"]}
    assert "TARBALL_NOT_VENDORED" in codes


# ---------------------------------------------------------------------------
# 1. Lock drift
# ---------------------------------------------------------------------------


def test_lock_range_drift_when_resolved_version_outside_range(builders):
    b = builders
    # Manifest wants ^1.2.0; lock pins 1.3.0 satisfies that — mutate lock to 2.0.0
    tar200 = b["make_tarball"]("left-pad", "2.0.0")
    key, node_data, vendor = b["node"]("left-pad", "2.0.0", tarball=tar200)
    lock = b["lockfile3"](
        {key: node_data},
        root_extra={"dependencies": {"left-pad": "^1.2.0"}},
    )
    report = verify_bundle(
        b["build_files"](manifest(deps={"left-pad": "^1.2.0"}), lock, [vendor])
    )
    codes = {f["code"]: f for f in report["findings"]}
    assert "LOCK_RANGE_DRIFT" in codes
    f = codes["LOCK_RANGE_DRIFT"]
    assert f["diff"]["kind"] == "range"
    assert f["diff"]["expected"] == "^1.2.0"
    assert f["chain"], "drift must carry a reviewable chain"
    assert report["status"] == "fail"


def test_lock_spec_drift_manifest_vs_lock_importer(builders):
    b = builders
    tar = b["make_tarball"]("left-pad", "1.3.0")
    key, node_data, vendor = b["node"]("left-pad", "1.3.0", tarball=tar)
    lock = b["lockfile3"](
        {key: node_data},
        root_extra={"dependencies": {"left-pad": "1.2.0"}},
    )
    report = verify_bundle(
        b["build_files"](manifest(deps={"left-pad": "1.3.0"}), lock, [vendor])
    )
    codes = {f["code"] for f in report["findings"]}
    assert "LOCK_SPEC_DRIFT" in codes
    assert report["status"] == "fail"


def test_tampered_tarball_is_cryptographic_drift(builders):
    b = builders
    good = bytearray(b["make_tarball"]("left-pad", "1.3.0"))
    good[-50] ^= 0x01  # flip a byte after the integrity was computed
    tampered = bytes(good)
    original = b["make_tarball"]("left-pad", "1.3.0")
    key, node_data, vendor = b["node"](
        "left-pad", "1.3.0", tarball=original
    )
    # Overwrite vendored bytes with tampered payload, keep lock integrity.
    files = b["build_files"](
        manifest(deps={"left-pad": "^1.3.0"}),
        b["lockfile3"](
            {key: node_data},
            root_extra={"dependencies": {"left-pad": "^1.3.0"}},
        ),
        [],
    )
    files[vendor[0]] = tampered
    report = verify_bundle(files)
    codes = {f["code"] for f in report["findings"]}
    assert "TARBALL_INTEGRITY_MISMATCH" in codes
    assert report["reproducibility"]["status"] is False


def test_unsupported_lockfile_versions_rejected(builders):
    b = builders
    pkg = manifest(deps={})
    lock_v1 = b'{"name":"x","version":"1.0.0","lockfileVersion":1,"dependencies":{}}'
    files = {"package.json": pkg, "package-lock.json": lock_v1}
    report = verify_bundle(files)
    codes = {f["code"] for f in report["findings"]}
    assert "UNSUPPORTED_LOCK_VERSION" in codes
    assert report["status"] == "fail"


def test_unsupported_dependency_spec_rejected(builders):
    b = builders
    pkg = manifest(deps={"evil": "github:evil/owner#deadbeef"})
    lock = b["lockfile3"](
        {},
        root_extra={"dependencies": {"evil": "github:evil/owner#deadbeef"}},
    )
    report = verify_bundle(b["build_files"](pkg, lock, []))
    codes = {f["code"] for f in report["findings"]}
    assert "UNSUPPORTED_RANGE" in codes
    assert report["status"] == "fail"


def test_foreign_lockfiles_are_refused(builders):
    b = builders
    files = b["build_files"](
        manifest(),
        b["lockfile3"]({}),
        [],
        extra={"yarn.lock": b"# yarn lockfile", "pnpm-lock.yaml": b"lockfileVersion: '6.0'"},
    )
    report = verify_bundle(files)
    codes = {f["code"] for f in report["findings"]}
    assert "UNSUPPORTED_LOCKFILE" in codes


# ---------------------------------------------------------------------------
# 2. Duplicate versions
# ---------------------------------------------------------------------------


def test_duplicate_same_version_identical_is_warning(builders):
    b = builders
    util = b["make_tarball"]("util-x", "1.0.0")
    key1, n1, v1 = b["node"]("util-x", "1.0.0", tarball=util)
    key2, n2, _ = b["node"](
        "util-x", "1.0.0", tarball=util
    )
    key2 = "node_modules/a/node_modules/util-x"
    a_tar = b["make_tarball"]("a", "1.0.0")
    a_key, a_node, a_vendor = b["node"](
        "a", "1.0.0", tarball=a_tar, deps={"util-x": "^1.0.0"}
    )
    root_tar = b["make_tarball"]("root-dep", "1.0.0")
    root_key, root_node, root_vendor = b["node"](
        "root-dep", "1.0.0", tarball=root_tar, deps={"util-x": "^1.0.0"}
    )
    lock = b["lockfile3"](
        {root_key: root_node, a_key: a_node, key1: n1, key2: n2},
        root_extra={"dependencies": {"root-dep": "^1.0.0", "a": "^1.0.0"}},
    )
    pkg = manifest(deps={"root-dep": "^1.0.0", "a": "^1.0.0"})
    report = verify_bundle(
        b["build_files"](pkg, lock, [v1, a_vendor, root_vendor])
    )
    codes = {f["code"]: f for f in report["findings"]}
    assert "DUPLICATE_VERSION_COPIES" in codes
    assert codes["DUPLICATE_VERSION_COPIES"]["severity"] == "warning"


def test_duplicate_same_version_conflicting_integrity_is_error(builders):
    b = builders
    good = b["make_tarball"]("util-x", "1.0.0")
    evil = b["make_tarball"]("util-x", "1.0.0", extra_files=[("evil.js", b"different")])
    assert good != evil
    k1, n1, v1 = b["node"]("util-x", "1.0.0", tarball=good)
    k2, n2, v2 = b["node"]("util-x", "1.0.0", tarball=evil)
    k2 = "node_modules/a/node_modules/util-x"
    a_tar = b["make_tarball"]("a", "1.0.0")
    a_key, a_node, a_vendor = b["node"](
        "a", "1.0.0", tarball=a_tar, deps={"util-x": "^1.0.0"}
    )
    root_tar = b["make_tarball"]("root-dep", "1.0.0")
    root_key, root_node, root_vendor = b["node"](
        "root-dep", "1.0.0", tarball=root_tar, deps={"util-x": "^1.0.0"}
    )
    lock = b["lockfile3"](
        {root_key: root_node, a_key: a_node, k1: n1, k2: n2},
        root_extra={"dependencies": {"root-dep": "^1.0.0", "a": "^1.0.0"}},
    )
    pkg = manifest(deps={"root-dep": "^1.0.0", "a": "^1.0.0"})
    report = verify_bundle(
        b["build_files"](pkg, lock, [v1, v2, a_vendor, root_vendor])
    )
    codes = {f["code"]: f for f in report["findings"]}
    assert "CONFLICTING_DUPLICATES" in codes
    assert codes["CONFLICTING_DUPLICATES"]["severity"] == "error"
    assert report["status"] == "fail"


def test_multiple_distinct_versions_are_info_not_failure(builders):
    b = builders
    v100 = b["make_tarball"]("util-x", "1.0.0")
    v200 = b["make_tarball"]("util-x", "2.0.0")
    k1, n1, vend1 = b["node"]("util-x", "1.0.0", tarball=v100)
    k2, n2, vend2 = b["node"]("util-x", "2.0.0", tarball=v200)
    k2 = "node_modules/a/node_modules/util-x"
    a_tar = b["make_tarball"]("a", "1.0.0")
    a_key, a_node, a_vendor = b["node"](
        "a", "1.0.0", tarball=a_tar, deps={"util-x": "^2.0.0"}
    )
    lock = b["lockfile3"](
        {a_key: a_node, k1: n1, k2: n2},
        root_extra={"dependencies": {"util-x": "^1.0.0", "a": "^1.0.0"}},
    )
    pkg = manifest(deps={"util-x": "^1.0.0", "a": "^1.0.0"})
    report = verify_bundle(b["build_files"](pkg, lock, [vend1, vend2, a_vendor]))
    codes = {f["code"]: f for f in report["findings"]}
    assert "MULTIPLE_VERSIONS" in codes
    assert codes["MULTIPLE_VERSIONS"]["severity"] == "info"
    assert report["status"] == "pass"


# ---------------------------------------------------------------------------
# 3. Missing integrity
# ---------------------------------------------------------------------------


def test_missing_integrity_is_error(builders):
    b = builders
    tar = b["make_tarball"]("left-pad", "1.3.0")
    key, node_data, vendor = b["node"](
        "left-pad", "1.3.0", tarball=tar, integrity=False
    )
    lock = b["lockfile3"](
        {key: node_data},
        root_extra={"dependencies": {"left-pad": "^1.3.0"}},
    )
    report = verify_bundle(
        b["build_files"](manifest(deps={"left-pad": "^1.3.0"}), lock, [vendor])
    )
    codes = {f["code"] for f in report["findings"]}
    assert "MISSING_INTEGRITY" in codes
    # Hash verification cannot have run for the unauthenticated node.
    assert report["summary"]["vendored_tarballs_verified"] == 0


def test_sha1_integrity_is_weak_warning(builders):
    b = builders
    tar = b["make_tarball"]("legacy", "1.0.0")
    key, node_data, vendor = b["node"](
        "legacy", "1.0.0", tarball=tar, integrity=b["npm_integrity"](tar, "sha1")
    )
    lock = b["lockfile3"](
        {key: node_data},
        root_extra={"dependencies": {"legacy": "^1.0.0"}},
    )
    report = verify_bundle(
        b["build_files"](manifest(deps={"legacy": "^1.0.0"}), lock, [vendor])
    )
    codes = {f["code"]: f for f in report["findings"]}
    assert "WEAK_INTEGRITY" in codes
    assert codes["WEAK_INTEGRITY"]["severity"] == "warning"
    assert report["reproducibility"]["status"] is False


# ---------------------------------------------------------------------------
# 4. Platform optional dependencies
# ---------------------------------------------------------------------------


def _platform_graph(builders):
    b = builders
    native = b["make_tarball"]("native-helper", "1.0.0")
    k, n, vendor = b["node"](
        "native-helper",
        "1.0.0",
        tarball=native,
        os_constraints=["darwin"],
        cpu_constraints=["arm64"],
        optional=True,
    )
    lock = b["lockfile3"](
        {k: n},
        root_extra={"optionalDependencies": {"native-helper": "^1.0.0"}},
    )
    pkg = manifest(optional_deps={"native-helper": "^1.0.0"})
    return b["build_files"](pkg, lock, [vendor])


def test_optional_native_pkg_excluded_on_linux_is_info(builders):
    report = verify_bundle(_platform_graph(builders), os_name="linux", cpu="x64")
    codes = {f["code"]: f for f in report["findings"]}
    assert "PLATFORM_EXCLUDED_OPTIONAL" in codes
    assert codes["PLATFORM_EXCLUDED_OPTIONAL"]["severity"] == "info"
    assert report["status"] == "pass"


def test_optional_native_pkg_included_on_matching_platform(builders):
    report = verify_bundle(_platform_graph(builders), os_name="darwin", cpu="arm64")
    codes = {f["code"] for f in report["findings"]}
    assert "PLATFORM_EXCLUDED_OPTIONAL" not in codes
    assert report["summary"]["vendored_tarballs_verified"] == 1
    assert report["status"] == "pass"


def test_required_platform_mismatch_is_error(builders):
    b = builders
    native = b["make_tarball"]("must-have", "1.0.0")
    k, n, vendor = b["node"](
        "must-have", "1.0.0", tarball=native, os_constraints=["win32"]
    )
    lock = b["lockfile3"](
        {k: n}, root_extra={"dependencies": {"must-have": "^1.0.0"}}
    )
    report = verify_bundle(
        b["build_files"](manifest(deps={"must-have": "^1.0.0"}), lock, [vendor]),
        os_name="linux",
    )
    codes = {f["code"] for f in report["findings"]}
    assert "PLATFORM_MISMATCH" in codes
    assert report["status"] == "fail"


def test_optional_dep_absent_from_lock_is_allowed(builders):
    b = builders
    # optionalDependency declared by importer but no node exists at all
    lock = b["lockfile3"](
        {},
        root_extra={"optionalDependencies": {"maybe": "^1.0.0"}},
    )
    report = verify_bundle(
        b["build_files"](
            manifest(optional_deps={"maybe": "^1.0.0"}), lock, []
        )
    )
    codes = {f["code"]: f for f in report["findings"]}
    assert "OPTIONAL_DEP_MISSING" in codes
    assert codes["OPTIONAL_DEP_MISSING"]["severity"] == "info"
    assert report["status"] == "pass"


def test_optional_peer_dependency_modelled(builders):
    b = builders
    peer_tar = b["make_tarball"]("peer-thing", "3.0.0")
    k, n, vendor = b["node"](
        "peer-thing", "3.0.0", tarball=peer_tar
    )
    host_tar = b["make_tarball"]("host", "1.0.0")
    hk, hn, hvendor = b["node"](
        "host",
        "1.0.0",
        tarball=host_tar,
        peer_deps={"peer-thing": "^3.0.0"},
        peer_optional={"peer-thing"},
    )
    lock = b["lockfile3"](
        {hk: hn, k: n},
        root_extra={"dependencies": {"host": "^1.0.0"}},
    )
    files = b["build_files"](
        manifest(deps={"host": "^1.0.0"}), lock, [hvendor, vendor]
    )
    # Remove the optional peer entirely (lock + bytes): still acceptable.
    del files[vendor[0]]
    lock_doc = json.loads(files["package-lock.json"].decode())
    del lock_doc["packages"][k]
    files["package-lock.json"] = (json.dumps(lock_doc, indent=2) + "\n").encode()
    report = verify_bundle(files)
    assert report["status"] == "pass", json.dumps(report, indent=2)


# ---------------------------------------------------------------------------
# 5. Circular dependencies
# ---------------------------------------------------------------------------


def test_direct_cycle_is_reported_with_shortest_chain(builders):
    b = builders
    a_tar = b["make_tarball"]("cyc-a", "1.0.0")
    c_tar = b["make_tarball"]("cyc-c", "1.0.0")
    ak, an, av = b["node"](
        "cyc-a", "1.0.0", tarball=a_tar, deps={"cyc-c": "^1.0.0"}
    )
    ck, cn, cv = b["node"](
        "cyc-c", "1.0.0", tarball=c_tar, deps={"cyc-a": "^1.0.0"}
    )
    lock = b["lockfile3"](
        {ak: an, ck: cn},
        root_extra={"dependencies": {"cyc-a": "^1.0.0"}},
    )
    report = verify_bundle(
        b["build_files"](manifest(deps={"cyc-a": "^1.0.0"}), lock, [av, cv])
    )
    cycles = [f for f in report["findings"] if f["code"] == "DEPENDENCY_CYCLE"]
    assert len(cycles) == 1
    message = cycles[0]["message"]
    assert "cyc-a" in message and "cyc-c" in message
    assert cycles[0]["severity"] == "warning"
    # Traversal must terminate despite the cycle:
    assert report["summary"]["visited_nodes"] == 2


def test_self_cycle_is_reported(builders):
    b = builders
    tar = b["make_tarball"]("selfish", "1.0.0")
    k, n, vendor = b["node"](
        "selfish", "1.0.0", tarball=tar, deps={"selfish": "^1.0.0"}
    )
    lock = b["lockfile3"](
        {k: n}, root_extra={"dependencies": {"selfish": "^1.0.0"}}
    )
    report = verify_bundle(
        b["build_files"](manifest(deps={"selfish": "^1.0.0"}), lock, [vendor])
    )
    assert any(f["code"] == "DEPENDENCY_CYCLE" for f in report["findings"])


# ---------------------------------------------------------------------------
# Relationship / resolution
# ---------------------------------------------------------------------------


def test_missing_transitive_lock_entry_is_error(builders):
    b = builders
    host = b["make_tarball"]("host", "1.0.0")
    hk, hn, hvendor = b["node"](
        "host", "1.0.0", tarball=host, deps={"ghost": "^1.0.0"}
    )
    lock = b["lockfile3"](
        {hk: hn}, root_extra={"dependencies": {"host": "^1.0.0"}}
    )
    report = verify_bundle(
        b["build_files"](manifest(deps={"host": "^1.0.0"}), lock, [hvendor])
    )
    codes = {f["code"]: f for f in report["findings"]}
    assert "MISSING_LOCK_ENTRY" in codes
    chain = codes["MISSING_LOCK_ENTRY"]["chain"]
    assert any("host" in step for step in chain)
    assert report["status"] == "fail"


def test_nested_node_modules_resolution(builders):
    b = builders
    inner = b["make_tarball"]("util-x", "2.0.0")
    outer = b["make_tarball"]("util-x", "1.0.0")
    ik, inn, iv = b["node"]("util-x", "2.0.0", tarball=inner)
    ik = "node_modules/host/node_modules/util-x"
    ok, onn, ov = b["node"]("util-x", "1.0.0", tarball=outer)
    host_tar = b["make_tarball"]("host", "1.0.0")
    hk, hn, hv = b["node"](
        "host", "1.0.0", tarball=host_tar, deps={"util-x": "^2.0.0"}
    )
    lock = b["lockfile3"](
        {hk: hn, ok: onn, ik: inn},
        root_extra={"dependencies": {"host": "^1.0.0", "util-x": "^1.0.0"}},
    )
    report = verify_bundle(
        b["build_files"](
            manifest(deps={"host": "^1.0.0", "util-x": "^1.0.0"}),
            lock,
            [hv, ov, iv],
        )
    )
    assert report["status"] == "pass", json.dumps(report, indent=2)


def test_install_script_is_flagged_but_not_run(builders):
    b = builders
    tar = b["make_tarball"](
        "scripty", "1.0.0", scripts={"postinstall": "node nope.js"}
    )
    k, n, vendor = b["node"](
        "scripty", "1.0.0", tarball=tar, has_install_script=True
    )
    lock = b["lockfile3"](
        {k: n}, root_extra={"dependencies": {"scripty": "^1.0.0"}}
    )
    report = verify_bundle(
        b["build_files"](manifest(deps={"scripty": "^1.0.0"}), lock, [vendor])
    )
    codes = {f["code"]: f for f in report["findings"]}
    assert "INSTALL_SCRIPT_PRESENT" in codes
    assert report["reproducibility"]["status"] is False


def test_insecure_resolved_url_rejected(builders):
    b = builders
    tar = b["make_tarball"]("plain", "1.0.0")
    k, n, vendor = b["node"](
        "plain", "1.0.0", tarball=tar, resolved="http://registry.example/plain-1.0.0.tgz"
    )
    lock = b["lockfile3"](
        {k: n}, root_extra={"dependencies": {"plain": "^1.0.0"}}
    )
    report = verify_bundle(
        b["build_files"](manifest(deps={"plain": "^1.0.0"}), lock, [vendor])
    )
    codes = {f["code"] for f in report["findings"]}
    assert "INSECURE_RESOLVED_URL" in codes


def test_node_modules_tree_upload_rejected(builders):
    b = builders
    files = b["build_files"](
        manifest(), b["lockfile3"]({}), [],
        extra={"node_modules/left-pad/index.js": b"module.exports={}"},
    )
    report = verify_bundle(files)
    codes = {f["code"] for f in report["findings"]}
    assert "UNSUPPORTED_BUNDLE_LAYOUT" in codes


def test_production_flag_excludes_dev_dependencies(builders):
    b = builders
    dev_tar = b["make_tarball"]("devtool", "1.0.0")
    k, n, vendor = b["node"]("devtool", "1.0.0", tarball=dev_tar)
    lock = b["lockfile3"](
        {k: n},
        root_extra={"devDependencies": {"devtool": "^1.0.0"}},
    )
    pkg = manifest(dev_deps={"devtool": "^1.0.0"})
    files = b["build_files"](pkg, lock, [vendor])
    full = verify_bundle(files)
    assert full["status"] == "pass"
    assert full["summary"]["visited_nodes"] == 1
    prod = verify_bundle(files, production=True)
    assert prod["summary"]["visited_nodes"] == 0
