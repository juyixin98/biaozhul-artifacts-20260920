"""End-to-end auditor scenarios (the five required cases, plus more)."""
from __future__ import annotations

import json

import pytest

from app.lockgate.audit import audit
from app.lockgate.platforms import Target

from .conftest import make_tarball, registry_node, vendor_path


def _j(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def codes(result, severity=None):
    return {f["code"] for f in result["findings"]
            if severity is None or f["severity"] == severity}


def find(result, code):
    return next(f for f in result["findings"] if f["code"] == code)


# --------------------------------------------------------------------------
# clean baseline
# --------------------------------------------------------------------------

def test_clean_bundle_is_reproducible(make_bundle, blobs):
    b = blobs
    pkg = {"name": "app", "version": "1.0.0",
           "dependencies": {"left-pad": "^1.2.0", "debug": "^4.3.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "dependencies": {"left-pad": "^1.2.0", "debug": "^4.3.0"}},
                "node_modules/left-pad": registry_node("left-pad", "1.2.3", b.left_pad),
                "node_modules/debug": registry_node("debug", "4.3.4", b.debug,
                                                    deps={"ms": "^2.0.0"}),
                "node_modules/ms": registry_node("ms", "2.1.3", b.ms),
            }}
    files = {"package.json": _j(pkg), "package-lock.json": _j(lock),
             vendor_path("left-pad", "1.2.3"): b.left_pad,
             vendor_path("debug", "4.3.4"): b.debug,
             vendor_path("ms", "2.1.3"): b.ms}
    r = audit(make_bundle(files))
    assert r["ok"] is True
    assert r["reproducibility"]["reproducible"] is True
    assert r["reproducibility"]["content_verified"] is True
    assert r["summary"]["artifacts_verified"] == 3


# --------------------------------------------------------------------------
# lock drift
# --------------------------------------------------------------------------

def test_lock_drift_outside_range(make_bundle, blobs):
    # declared ~1.2.0 but lock pinned to 1.3.0
    blob = make_tarball("left-pad", "1.3.0")
    pkg = {"name": "app", "version": "1.0.0",
           "dependencies": {"left-pad": "~1.2.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "dependencies": {"left-pad": "~1.2.0"}},
                "node_modules/left-pad": registry_node("left-pad", "1.3.0", blob),
            }}
    r = audit(make_bundle({
        "package.json": _j(pkg), "package-lock.json": _j(lock),
        vendor_path("left-pad", "1.3.0"): blob}))
    assert "range-drift" in codes(r, "error")
    assert r["ok"] is False
    f = find(r, "range-drift")
    assert f["chain"] == ["app@1.0.0"]          # shortest chain to root
    assert f["evidence"]["resolved"] == "1.3.0"
    assert f["diff"] and "-" in f["diff"]       # reviewable diff present


def test_drift_in_nested_dependency(make_bundle, blobs):
    # debug declares ms ^2.0.0 in the lock but is pinned to ms 3.0.0
    b = blobs
    pkg = {"name": "app", "version": "1.0.0", "dependencies": {"debug": "^4.3.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "dependencies": {"debug": "^4.3.0"}},
                "node_modules/debug": registry_node("debug", "4.3.4", b.debug,
                                                    deps={"ms": "^2.0.0"}),
                "node_modules/ms": registry_node("ms", "3.0.0", b.ms3),
            }}
    r = audit(make_bundle({
        "package.json": _j(pkg), "package-lock.json": _j(lock),
        vendor_path("debug", "4.3.4"): b.debug,
        vendor_path("ms", "3.0.0"): b.ms3}))
    f = find(r, "range-drift")
    # shortest chain must show app -> debug -> ms
    assert f["chain"] == ["app@1.0.0", "debug@4.3.4"]


# --------------------------------------------------------------------------
# duplicate versions
# --------------------------------------------------------------------------

def test_duplicate_versions_reported(make_bundle, blobs):
    b = blobs
    pkg = {"name": "app", "version": "1.0.0",
           "dependencies": {"debug": "^4.3.0", "@scope/t": "^2.0.0"}}
    t = make_tarball("@scope/t", "2.0.0")
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "dependencies": {"debug": "^4.3.0", "@scope/t": "^2.0.0"}},
                "node_modules/debug": registry_node("debug", "4.3.4", b.debug,
                                                    deps={"ms": "^2.0.0"}),
                "node_modules/ms": registry_node("ms", "2.1.3", b.ms),
                "node_modules/@scope/t": registry_node("@scope/t", "2.0.0", t,
                                                       deps={"ms": "^3.0.0"}),
                "node_modules/@scope/t/node_modules/ms":
                    registry_node("ms", "3.0.0", b.ms3),
            }}
    r = audit(make_bundle({
        "package.json": _j(pkg), "package-lock.json": _j(lock),
        vendor_path("debug", "4.3.4"): b.debug,
        vendor_path("ms", "2.1.3"): b.ms,
        "vendor/t-2.0.0.tgz": t,
        vendor_path("ms", "3.0.0"): b.ms3}))
    assert r["summary"]["duplicate_names"]["ms"] == ["2.1.3", "3.0.0"]
    assert "duplicate-versions" in codes(r, "warning")
    # duplicates are not by themselves fatal
    assert r["ok"] is True


# --------------------------------------------------------------------------
# missing integrity
# --------------------------------------------------------------------------

def test_missing_integrity_is_fatal(make_bundle, blobs):
    b = blobs
    node = registry_node("left-pad", "1.2.3", b.left_pad)
    del node["integrity"]
    pkg = {"name": "app", "version": "1.0.0", "dependencies": {"left-pad": "^1.2.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {"": {"name": "app", "version": "1.0.0",
                              "dependencies": {"left-pad": "^1.2.0"}},
                         "node_modules/left-pad": node}}
    r = audit(make_bundle({"package.json": _j(pkg), "package-lock.json": _j(lock),
                           vendor_path("left-pad", "1.2.3"): b.left_pad}))
    assert "missing-or-bad-integrity" in codes(r, "error")
    assert r["reproducibility"]["pinned"] is False


def test_malformed_integrity_is_fatal(make_bundle, blobs):
    b = blobs
    node = registry_node("left-pad", "1.2.3", b.left_pad)
    node["integrity"] = "sha512-not-base64!!"
    pkg = {"name": "app", "version": "1.0.0", "dependencies": {"left-pad": "^1.2.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {"": {"name": "app", "version": "1.0.0",
                              "dependencies": {"left-pad": "^1.2.0"}},
                         "node_modules/left-pad": node}}
    r = audit(make_bundle({"package.json": _j(pkg), "package-lock.json": _j(lock)}))
    assert "missing-or-bad-integrity" in codes(r, "error")


# --------------------------------------------------------------------------
# platform optional dependencies
# --------------------------------------------------------------------------

def test_platform_optional_excluded_is_info_not_error(make_bundle):
    blob = make_tarball("win-x", "1.0.0", install=True)
    pkg = {"name": "app", "version": "1.0.0",
           "optionalDependencies": {"win-x": "^1.0.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "optionalDependencies": {"win-x": "^1.0.0"}},
                "node_modules/win-x": registry_node("win-x", "1.0.0", blob,
                                                    optional=True, os=["win32"],
                                                    hasInstallScript=True),
            }}
    r = audit(make_bundle({
        "package.json": _j(pkg), "package-lock.json": _j(lock),
        vendor_path("win-x", "1.0.0"): blob}),
        target=Target(os="linux", cpu="x64"))
    assert "platform-excluded" in codes(r, "info")
    assert "platform-required-excluded" not in {f["code"] for f in r["findings"]}
    # lifecycle scripts must be surfaced as *not run*
    assert "install-script-not-run" in codes(r)


def test_platform_required_but_excluded_is_error(make_bundle):
    # a package restricted to win32, pulled in as a *required* dependency,
    # audited for linux: must be an error, not silently skipped.
    blob = make_tarball("win-x", "1.0.0")
    pkg = {"name": "app", "version": "1.0.0", "dependencies": {"win-x": "^1.0.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "dependencies": {"win-x": "^1.0.0"}},
                "node_modules/win-x": registry_node("win-x", "1.0.0", blob,
                                                    os=["win32"]),
            }}
    r = audit(make_bundle({
        "package.json": _j(pkg), "package-lock.json": _j(lock),
        vendor_path("win-x", "1.0.0"): blob}),
        target=Target(os="linux", cpu="x64"))
    assert "platform-required-excluded" in codes(r, "error")


def test_optional_dependency_absent_on_platform_is_ok(make_bundle):
    # npm on linux simply omits the windows-only package from the lock tree;
    # a lock that legitimately lacks it must pass for that target.
    pkg = {"name": "app", "version": "1.0.0",
           "optionalDependencies": {"win-x": "^1.0.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "optionalDependencies": {"win-x": "^1.0.0"}},
            }}
    r = audit(make_bundle({"package.json": _j(pkg),
                           "package-lock.json": _j(lock)}),
              target=Target(os="linux", cpu="x64"))
    assert r["ok"] is True


# --------------------------------------------------------------------------
# cycles
# --------------------------------------------------------------------------

def test_dependency_cycle_detected(make_bundle):
    a = make_tarball("cyc-a", "1.0.0")
    b = make_tarball("cyc-b", "1.0.0")
    pkg = {"name": "app", "version": "1.0.0", "dependencies": {"cyc-a": "^1.0.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "dependencies": {"cyc-a": "^1.0.0"}},
                "node_modules/cyc-a": registry_node("cyc-a", "1.0.0", a,
                                                    deps={"cyc-b": "^1.0.0"}),
                "node_modules/cyc-b": registry_node("cyc-b", "1.0.0", b,
                                                    deps={"cyc-a": "^1.0.0"}),
            }}
    r = audit(make_bundle({
        "package.json": _j(pkg), "package-lock.json": _j(lock),
        vendor_path("cyc-a", "1.0.0"): a,
        vendor_path("cyc-b", "1.0.0"): b}))
    cyc = r["summary"]["cycles"]
    assert cyc and set(cyc[0]) == {"cyc-a@1.0.0", "cyc-b@1.0.0"}
    assert "dependency-cycle" in codes(r, "warning")
    f = find(r, "dependency-cycle")
    # chain explains how the cycle is reached from root
    assert f["chain"][0] == "app@1.0.0"


def test_self_cycle_detected(make_bundle):
    blob = make_tarball("selfish", "1.0.0")
    pkg = {"name": "app", "version": "1.0.0", "dependencies": {"selfish": "^1.0.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "dependencies": {"selfish": "^1.0.0"}},
                "node_modules/selfish": registry_node("selfish", "1.0.0", blob,
                                                      deps={"selfish": "^1.0.0"}),
            }}
    r = audit(make_bundle({
        "package.json": _j(pkg), "package-lock.json": _j(lock),
        vendor_path("selfish", "1.0.0"): blob}))
    assert r["summary"]["cycles"] == [["selfish@1.0.0"]]


# --------------------------------------------------------------------------
# unsupported syntax
# --------------------------------------------------------------------------

@pytest.mark.parametrize("spec", [
    "git+https://github.com/x/y.git#deadbee",
    "github:x/y",
    "file:../local",
    "npm:real-name@^1.0.0",
    "http://example.com/p.tgz",
])
def test_unsupported_root_spec_refused(make_bundle, spec):
    pkg = {"name": "app", "version": "1.0.0", "dependencies": {"thing": spec}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {"": {"name": "app", "version": "1.0.0",
                              "dependencies": {"thing": spec}}}}
    r = audit(make_bundle({"package.json": _j(pkg),
                           "package-lock.json": _j(lock)}))
    assert "unsupported-syntax" in codes(r, "error")
    assert any(spec in s for s in r["unsupported_syntax"])
    assert r["ok"] is False


def test_lockfile_v2_rejected(make_bundle):
    pkg = {"name": "app", "version": "1.0.0"}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 2,
            "dependencies": {}}
    r = audit(make_bundle({"package.json": _j(pkg),
                           "package-lock.json": _j(lock)}))
    assert "unsupported-syntax" in codes(r, "error")
    assert "lockfileVersion" in r["unsupported_syntax"][0]


# --------------------------------------------------------------------------
# workspaces
# --------------------------------------------------------------------------

def test_workspace_link_consistency(make_bundle, blobs):
    b = blobs
    root = {"name": "mono", "version": "0.0.0", "private": True,
            "workspaces": ["packages/*"],
            "dependencies": {"@mono/lib": "^1.0.0", "left-pad": "^1.2.0"}}
    lib = {"name": "@mono/lib", "version": "1.0.0", "dependencies": {"ms": "^2.0.0"}}
    lock = {"name": "mono", "version": "0.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "mono", "version": "0.0.0",
                     "workspaces": ["packages/*"],
                     "dependencies": {"@mono/lib": "^1.0.0",
                                      "left-pad": "^1.2.0"}},
                "node_modules/left-pad": registry_node("left-pad", "1.2.3", b.left_pad),
                "node_modules/ms": registry_node("ms", "2.1.3", b.ms),
                "node_modules/@mono/lib": {"resolved": "../../packages/lib",
                                           "link": True, "version": "1.0.0",
                                           "dependencies": {"ms": "^2.0.0"}},
                "packages/lib": {"name": "@mono/lib", "version": "1.0.0",
                                 "dependencies": {"ms": "^2.0.0"}},
            }}
    r = audit(make_bundle({
        "package.json": _j(root), "package-lock.json": _j(lock),
        "packages/lib/package.json": _j(lib),
        vendor_path("left-pad", "1.2.3"): b.left_pad,
        vendor_path("ms", "2.1.3"): b.ms}))
    assert r["ok"] is True, [f["message"] for f in r["findings"] if f["severity"] == "error"]
    assert r["summary"]["workspace_links"] == 1


def test_workspace_missing_lock_link(make_bundle):
    root = {"name": "mono", "version": "0.0.0", "workspaces": ["packages/*"]}
    lib = {"name": "@mono/lib", "version": "1.0.0"}
    lock = {"name": "mono", "version": "0.0.0", "lockfileVersion": 3,
            "packages": {"": {"name": "mono", "version": "0.0.0",
                              "workspaces": ["packages/*"]}}}
    r = audit(make_bundle({
        "package.json": _j(root), "package-lock.json": _j(lock),
        "packages/lib/package.json": _j(lib)}))
    assert "workspace-not-locked" in codes(r, "error")


# --------------------------------------------------------------------------
# the central thesis: a lockfile present is NOT reproducibility
# --------------------------------------------------------------------------

def test_lockfile_present_without_artifacts_not_reproducible(make_bundle, blobs):
    b = blobs
    pkg = {"name": "app", "version": "1.0.0", "dependencies": {"left-pad": "^1.2.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "dependencies": {"left-pad": "^1.2.0"}},
                "node_modules/left-pad": registry_node("left-pad", "1.2.3", b.left_pad),
            }}
    r = audit(make_bundle({"package.json": _j(pkg),
                           "package-lock.json": _j(lock)}))
    assert r["reproducibility"]["lockfile_present"] is True
    assert r["reproducibility"]["pinned"] is True
    assert r["reproducibility"]["content_verified"] is False
    assert r["reproducibility"]["reproducible"] is False
    assert any("not proof of reproducibility" in x
               for x in r["reproducibility"]["reasons"])


def test_tampered_artifact_fails_verification(make_bundle, blobs):
    b = blobs
    pkg = {"name": "app", "version": "1.0.0", "dependencies": {"left-pad": "^1.2.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "dependencies": {"left-pad": "^1.2.0"}},
                "node_modules/left-pad": registry_node("left-pad", "1.2.3", b.left_pad),
            }}
    attacker = make_tarball("left-pad", "1.2.3", payload=b"exports.pwned=1;\n")
    r = audit(make_bundle({
        "package.json": _j(pkg), "package-lock.json": _j(lock),
        vendor_path("left-pad", "1.2.3"): attacker}))
    assert "artifact-tampered" in codes(r, "error")
    f = find(r, "artifact-tampered")
    assert f["evidence"]["declared"] != f["evidence"]["actual"]
    assert r["reproducibility"]["reproducible"] is False


# --------------------------------------------------------------------------
# structural consistency
# --------------------------------------------------------------------------

def test_declared_but_not_locked(make_bundle):
    pkg = {"name": "app", "version": "1.0.0", "dependencies": {"ghost": "^1.0.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {"": {"name": "app", "version": "1.0.0",
                              "dependencies": {"ghost": "^1.0.0"}}}}
    r = audit(make_bundle({"package.json": _j(pkg),
                           "package-lock.json": _j(lock)}))
    assert "declared-not-locked" in codes(r, "error")


def test_locked_but_not_declared_warns(make_bundle, blobs):
    b = blobs
    pkg = {"name": "app", "version": "1.0.0"}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "dependencies": {"left-pad": "^1.2.0"}},
                "node_modules/left-pad": registry_node("left-pad", "1.2.3", b.left_pad),
            }}
    r = audit(make_bundle({
        "package.json": _j(pkg), "package-lock.json": _j(lock),
        vendor_path("left-pad", "1.2.3"): b.left_pad}))
    assert "locked-not-declared" in codes(r, "warning")


def test_missing_files_reported(make_bundle):
    r = audit(make_bundle({"README.md": b"# hi"}))
    assert r["ok"] is False
    assert any("missing" in f["message"] for f in r["findings"])


def test_untrusted_registry_host(make_bundle):
    blob = make_tarball("evil", "1.0.0")
    pkg = {"name": "app", "version": "1.0.0", "dependencies": {"evil": "^1.0.0"}}
    node = registry_node("evil", "1.0.0", blob)
    node["resolved"] = "https://evil.registry.example/evil-1.0.0.tgz"
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {"": {"name": "app", "version": "1.0.0",
                              "dependencies": {"evil": "^1.0.0"}},
                         "node_modules/evil": node}}
    r = audit(make_bundle({"package.json": _j(pkg),
                           "package-lock.json": _j(lock)}))
    assert "untrusted-registry" in codes(r, "error")


def test_extraneous_lock_node_warns(make_bundle, blobs):
    b = blobs
    ghost = make_tarball("ghost", "9.9.9")
    pkg = {"name": "app", "version": "1.0.0", "dependencies": {"left-pad": "^1.2.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "dependencies": {"left-pad": "^1.2.0"}},
                "node_modules/left-pad": registry_node("left-pad", "1.2.3", b.left_pad),
                "node_modules/ghost": registry_node("ghost", "9.9.9", ghost),
            }}
    r = audit(make_bundle({
        "package.json": _j(pkg), "package-lock.json": _j(lock),
        vendor_path("left-pad", "1.2.3"): b.left_pad,
        vendor_path("ghost", "9.9.9"): ghost}))
    assert "unreachable-node" in codes(r, "warning")
