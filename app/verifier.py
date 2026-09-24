"""Core offline dependency-lock consistency verification.

The verifier models exactly one lock format (npm ``package-lock.json``
``lockfileVersion: 3``) and one manifest (``package.json``). Everything else
is refused with an explicit finding rather than guessed at.

High-level checks performed:

1. Structure           — required files, strict JSON, single lock format,
                         node field shapes, rejected bundle layouts.
2. Importer agreement  — root and every workspace manifest agree with the
                         importer records in the lock (key sets and specs).
3. Resolution          — every declared dependency resolves by the real npm
                         node_modules walking algorithm to a lock node; the
                         node's exact version satisfies the declared range.
4. Graph relationships — dependency edges exist, platform-optional packages
                         are modelled per ``os``/``cpu``/``libc``, cycles are
                         reported, duplicate/conflicting versions detected.
5. Cryptography        — ``integrity`` is parsed and really recomputed with
                         hashlib over the vendored tarball; tarball manifests
                         are compared. Install scripts are reported, never run.
6. Reproducibility     — "a lock file exists" is never treated as proof;
                         offline reproducibility requires every artifact to be
                         present and hash-verified.
"""

from __future__ import annotations

import posixpath
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from difflib import unified_diff
from typing import Any, Iterable
from urllib.parse import urlparse

from .integrity import (
    IntegrityError,
    inspect_tarball,
    parse_integrity,
    verify_tarball,
)
from .semver import (
    InvalidVersionError,
    UnsupportedRangeError,
    Version,
    parse_range,
)
from .workspace import expand_workspaces

VALID_OS = {"aix", "darwin", "freebsd", "linux", "openbsd", "sunos", "win32", "android"}
VALID_CPU = {
    "arm",
    "arm64",
    "ia32",
    "mips",
    "mipsel",
    "ppc",
    "ppc64",
    "s390",
    "s390x",
    "x64",
}
VALID_LIBC = {"glibc", "musl"}

DEP_KINDS = ("dependencies", "optionalDependencies", "peerDependencies")
IMPORTER_KINDS = (
    "dependencies",
    "optionalDependencies",
    "peerDependencies",
    "devDependencies",
)


class VerifierInputError(ValueError):
    """Fatal input error (missing files, wrong lock format) carried as report."""


# ---------------------------------------------------------------------------
# Internal structures
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    code: str
    severity: str  # error | warning | info
    subject: str
    message: str
    file: str | None = None
    chain_subject: str | None = None  # graph key used for chain resolution
    diff_kind: str | None = None
    expected: Any = None
    actual: Any = None
    note: str | None = None


@dataclass
class LockNode:
    key: str
    data: dict[str, Any]
    name: str
    is_link: bool
    is_workspace: bool
    link_target: str | None
    version: str | None
    resolved: str | None
    integrity: str | None
    in_bundle: bool
    optional: bool
    dev: bool
    os: list[str] | None
    cpu: list[str] | None
    libc: list[str] | None
    has_install_script: bool


@dataclass
class Edge:
    owner: str
    dep_name: str
    spec: str
    kind: str
    edge_optional: bool
    target: str | None  # resolved lock key


@dataclass
class Workspace:
    directory: str
    name: str
    version: str | None
    manifest: dict[str, Any]


@dataclass
class _Platform:
    os: str
    cpu: str
    libc: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unified(expected: Any, actual: Any, expected_name: str, actual_name: str) -> str:
    exp_lines = (str(expected) + "\n").splitlines(keepends=True)
    act_lines = (str(actual) + "\n").splitlines(keepends=True)
    return "".join(
        unified_diff(
            exp_lines,
            act_lines,
            fromfile=expected_name,
            tofile=actual_name,
            n=2,
        )
    )


def _name_from_key(key: str) -> str:
    base = key
    if base.startswith("@") and base.count("/") >= 2:
        base = "/".join(base.split("/")[-2:])
    elif "/" in base:
        base = base.split("/")[-1]
    return base


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class Verifier:
    def __init__(
        self,
        files: dict[str, bytes],
        platform: _Platform,
        *,
        production: bool = False,
        require_vendored_tarballs: bool = False,
    ) -> None:
        self.files = files
        self.platform = platform
        self.production = production
        self.require_vendored_tarballs = require_vendored_tarballs

        self.findings: list[Finding] = []
        self.nodes: dict[str, LockNode] = {}
        self.workspaces: dict[str, Workspace] = {}  # directory -> workspace
        self.ws_by_name: dict[str, Workspace] = {}
        self.edges_out: dict[str, list[Edge]] = defaultdict(list)
        self.edge_index: dict[tuple[str, str], Edge] = {}
        self.visited: set[str] = set()
        # Every lock key an edge resolved to — including platform-excluded
        # optional nodes — so they are not later flagged as extraneous.
        self.reached: set[str] = set()
        self.verified_tarballs = 0
        self.missing_tarballs = 0
        self.weak_algorithms = 0

    # -- public entry point -------------------------------------------------

    def verify(self) -> dict[str, Any]:
        manifest, lock = self._load_documents()
        self._reject_foreign_lockfiles()
        self._reject_node_modules_layout()
        self._check_lock_format(lock)
        self._model_workspaces(manifest, lock)
        self._build_nodes(lock)
        self._check_roots(manifest, lock)
        self._check_workspace_importers(lock)
        self._traverse_graph()
        self._check_extraneous()
        self._check_duplicates()
        self._check_cycles()
        return self._build_report(lock)

    # -- finding helpers ----------------------------------------------------

    def error(self, **kwargs: Any) -> None:
        self.findings.append(Finding(severity="error", **kwargs))

    def warn(self, **kwargs: Any) -> None:
        self.findings.append(Finding(severity="warning", **kwargs))

    def info(self, **kwargs: Any) -> None:
        self.findings.append(Finding(severity="info", **kwargs))

    # -- loading -------------------------------------------------------------

    def _read_json(self, path: str) -> Any:
        from .extract import BundleError, json_loads_strict

        if path not in self.files:
            raise VerifierInputError(f"required file {path!r} not found in bundle")
        try:
            return json_loads_strict(self.files[path], path)
        except BundleError as exc:
            raise VerifierInputError(str(exc)) from exc

    def _load_documents(self) -> tuple[dict[str, Any], dict[str, Any]]:
        manifest = self._read_json("package.json")
        if not isinstance(manifest, dict):
            raise VerifierInputError("package.json root must be a JSON object")
        lock = self._read_json("package-lock.json")
        if not isinstance(lock, dict):
            raise VerifierInputError("package-lock.json root must be a JSON object")
        return manifest, lock

    def _reject_foreign_lockfiles(self) -> None:
        for path in sorted(self.files):
            base = posixpath.basename(path)
            if "/" not in path and base in {
                "yarn.lock",
                "pnpm-lock.yaml",
                "npm-shrinkwrap.json",
            }:
                self.error(
                    code="UNSUPPORTED_LOCKFILE",
                    subject=path,
                    file=path,
                    message=(
                        f"{base} is not supported; this gate accepts only npm "
                        "package-lock.json with lockfileVersion 3"
                    ),
                )

    def _reject_node_modules_layout(self) -> None:
        offenders = [p for p in self.files if p.startswith("node_modules/")]
        if offenders:
            self.error(
                code="UNSUPPORTED_BUNDLE_LAYOUT",
                subject="node_modules/",
                file=offenders[0],
                message=(
                    "bundle contains an unpacked node_modules/ tree; the gate "
                    "consumes manifests, package-lock.json and vendor/ tarballs "
                    "only — install scripts must never be executed to verify a "
                    "lock, and unpacked trees bypass integrity checks"
                ),
            )

    def _check_lock_format(self, lock: dict[str, Any]) -> None:
        version = lock.get("lockfileVersion")
        if not isinstance(version, int) or isinstance(version, bool):
            self.error(
                code="MALFORMED_LOCK",
                subject="package-lock.json",
                file="package-lock.json",
                message="lockfileVersion is missing or not an integer",
                expected=3,
                actual=version,
                diff_kind="value",
            )
            return
        if version != 3:
            self.error(
                code="UNSUPPORTED_LOCK_VERSION",
                subject="package-lock.json",
                file="package-lock.json",
                message=(
                    f"lockfileVersion {version} is not supported; only version 3 "
                    "is accepted. Regenerate with a supported npm "
                    "(`npm install --package-lock-only`)"
                ),
                expected=3,
                actual=version,
                diff_kind="value",
            )
        packages = lock.get("packages")
        if not isinstance(packages, dict):
            self.error(
                code="MALFORMED_LOCK",
                subject="packages",
                file="package-lock.json",
                message="lock file is missing the v3 'packages' object",
            )

    # -- workspaces ----------------------------------------------------------

    def _model_workspaces(
        self, manifest: dict[str, Any], lock: dict[str, Any]
    ) -> None:
        declared = manifest.get("workspaces")
        if isinstance(declared, dict):
            declared = declared.get("packages")
        dirs_with_manifest = {
            posixpath.dirname(path)
            for path in self.files
            if path.endswith("/package.json")
            and not path.startswith("vendor/")
            and not path.startswith("node_modules/")
        }
        dirs_with_manifest.discard("")

        matches, errors = expand_workspaces(declared, dirs_with_manifest)
        for err in errors:
            self.error(
                code="WORKSPACE_PATTERN_UNSUPPORTED",
                subject="package.json:workspaces",
                file="package.json",
                message=err,
            )
        for _pattern, directory in matches:
            self._load_workspace(directory)

    def _load_workspace(self, directory: str) -> None:
        from .extract import BundleError, json_loads_strict

        path = f"{directory}/package.json"
        try:
            ws_manifest = json_loads_strict(self.files[path], path)
        except BundleError as exc:
            self.error(
                code="MALFORMED_MANIFEST",
                subject=directory,
                file=path,
                message=f"workspace manifest unreadable: {exc}",
            )
            return
        if not isinstance(ws_manifest, dict):
            self.error(
                code="MALFORMED_MANIFEST",
                subject=directory,
                file=path,
                message="workspace package.json root must be an object",
            )
            return
        name = ws_manifest.get("name")
        version = ws_manifest.get("version")
        if not isinstance(name, str) or not name:
            self.error(
                code="WORKSPACE_WITHOUT_NAME",
                subject=directory,
                file=path,
                message="workspace package.json must declare a string 'name'",
            )
            return
        ws = Workspace(
            directory=directory,
            name=name,
            version=version if isinstance(version, str) else None,
            manifest=ws_manifest,
        )
        self.workspaces[directory] = ws
        if name in self.ws_by_name and self.ws_by_name[name].directory != directory:
            self.error(
                code="DUPLICATE_WORKSPACE_NAME",
                subject=name,
                file=path,
                message=(
                    f"workspace name {name!r} is declared by both "
                    f"{self.ws_by_name[name].directory!r} and {directory!r}"
                ),
            )
        self.ws_by_name[name] = ws

    # -- node model ----------------------------------------------------------

    def _build_nodes(self, lock: dict[str, Any]) -> None:
        packages = lock.get("packages")
        if not isinstance(packages, dict):
            return
        for key, raw in packages.items():
            if not isinstance(key, str):
                self.error(
                    code="MALFORMED_LOCK",
                    subject="<non-string key>",
                    file="package-lock.json",
                    message="packages keys must be strings",
                )
                continue
            if not isinstance(raw, dict):
                self.error(
                    code="MALFORMED_LOCK",
                    subject=key or "<root>",
                    file="package-lock.json",
                    message="packages entry must be an object",
                )
                continue
            self._build_one_node(key, raw)

    def _build_one_node(self, key: str, data: dict[str, Any]) -> None:
        is_link = bool(data.get("link"))
        resolved = data.get("resolved")
        is_workspace = key != "" and not key.startswith("node_modules/")
        if is_workspace and key not in self.workspaces:
            # A non-node_modules key that glob resolution did not surface means
            # the lock references a directory the manifest does not declare.
            self.error(
                code="LOCK_WORKSPACE_UNDECLARED",
                subject=key,
                file="package-lock.json",
                message=(
                    f"lock describes workspace directory {key!r} but package.json "
                    "'workspaces' does not include it"
                ),
            )
        name = data.get("name")
        if not isinstance(name, str) or not name:
            if key == "":
                name = ""
            elif is_workspace:
                name = self.workspaces.get(key).name if key in self.workspaces else _name_from_key(key)
            else:
                name = _name_from_key(key)

        def _str_or_none(value: Any) -> str | None:
            return value if isinstance(value, str) else None

        def _str_list(value: Any) -> list[str] | None:
            if value is None:
                return None
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                return list(value)
            self.error(
                code="MALFORMED_LOCK",
                subject=key or "<root>",
                file="package-lock.json",
                message="os/cpu/libc constraints must be arrays of strings",
            )
            return None

        node = LockNode(
            key=key,
            data=data,
            name=name,
            is_link=is_link,
            is_workspace=is_workspace,
            link_target=_str_or_none(resolved) if is_link else None,
            version=_str_or_none(data.get("version")),
            resolved=_str_or_none(resolved) if not is_link else None,
            integrity=_str_or_none(data.get("integrity")),
            in_bundle=bool(data.get("inBundle")),
            optional=bool(data.get("optional")),
            dev=bool(data.get("dev")),
            os=_str_list(data.get("os")),
            cpu=_str_list(data.get("cpu")),
            libc=_str_list(data.get("libc")),
            has_install_script=bool(data.get("hasInstallScript")),
        )
        self.nodes[key] = node
        if key != "" and not is_link and not is_workspace and not node.in_bundle:
            if not node.resolved:
                self.error(
                    code="MISSING_RESOLVED",
                    subject=key,
                    file="package-lock.json",
                    message="registry node has no 'resolved' tarball URL",
                    chain_subject=key,
                )
            else:
                self._check_resolved_url(node)

    def _check_resolved_url(self, node: LockNode) -> None:
        parsed = urlparse(node.resolved or "")
        if parsed.scheme != "https" or not parsed.netloc:
            self.error(
                code="INSECURE_RESOLVED_URL",
                subject=node.key,
                file="package-lock.json",
                message=(
                    f"resolved URL {node.resolved!r} must be an https URL; plain "
                    "http or file references cannot be authenticated offline"
                ),
                chain_subject=node.key,
                expected="https://registry.example/...tgz",
                actual=node.resolved,
                diff_kind="value",
            )

    # -- importer (root + workspace) agreement ------------------------------

    def _dep_map(self, obj: dict[str, Any], kind: str) -> dict[str, str]:
        value = obj.get(kind)
        if value is None:
            return {}
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise ValueError(f"{kind} must be an object of string->string")
        return dict(value)

    def _check_roots(self, manifest: dict[str, Any], lock: dict[str, Any]) -> None:
        root_node = self.nodes.get("")
        if root_node is None:
            self.error(
                code="MALFORMED_LOCK",
                subject="<root>",
                file="package-lock.json",
                message="lock file is missing the root packages[''] entry",
            )
            return

        # The root node is a self-description of the project; npm keeps name
        # and version synchronized with package.json.
        manifest_version = manifest.get("version")
        if isinstance(manifest_version, str) and root_node.version != manifest_version:
            self.error(
                code="ROOT_VERSION_DRIFT",
                subject="",
                file="package-lock.json",
                message="project version differs between manifest and lock",
                expected=manifest_version,
                actual=root_node.version,
                diff_kind="value",
            )

        kinds = ("dependencies", "optionalDependencies", "peerDependencies") + (
            ("devDependencies",) if not self.production else ()
        )
        self._compare_importer_maps(
            owner_key="",
            label="root",
            manifest=manifest,
            node_data=root_node.data,
            kinds=kinds,
        )
        self._reject_overrides_and_bad_specs("", manifest)

    def _check_workspace_importers(self, lock: dict[str, Any]) -> None:
        for directory, ws in sorted(self.workspaces.items()):
            node = self.nodes.get(directory)
            if node is None:
                self.error(
                    code="MISSING_WORKSPACE_LOCK_NODE",
                    subject=directory,
                    file=f"{directory}/package.json",
                    message=(
                        f"workspace {directory!r} ({ws.name}) has no "
                        f"packages[{directory!r}] importer entry in the lock"
                    ),
                )
                continue
            if not node.is_workspace:
                self.error(
                    code="MALFORMED_LOCK",
                    subject=directory,
                    file="package-lock.json",
                    message="workspace lock entry is not modelled as a workspace",
                )
            if ws.version is not None and node.version != ws.version:
                self.error(
                    code="WORKSPACE_VERSION_DRIFT",
                    subject=directory,
                    file="package-lock.json",
                    message=(
                        f"workspace {ws.name} version differs between manifest "
                        "and lock"
                    ),
                    expected=ws.version,
                    actual=node.version,
                    diff_kind="value",
                )
            self._compare_importer_maps(
                owner_key=directory,
                label=f"workspace:{directory}",
                manifest=ws.manifest,
                node_data=node.data,
                kinds=(
                    "dependencies",
                    "optionalDependencies",
                    "peerDependencies",
                )
                + (("devDependencies",) if not self.production else ()),
            )
            self._reject_overrides_and_bad_specs(directory, ws.manifest)

    def _compare_importer_maps(
        self,
        owner_key: str,
        label: str,
        manifest: dict[str, Any],
        node_data: dict[str, Any],
        kinds: Iterable[str],
    ) -> None:
        for kind in kinds:
            try:
                manifest_deps = self._dep_map(manifest, kind)
            except ValueError as exc:
                self.error(
                    code="MALFORMED_MANIFEST",
                    subject=f"{label}:{kind}",
                    message=str(exc),
                )
                continue
            try:
                lock_deps = self._dep_map(node_data, kind)
            except ValueError as exc:
                self.error(
                    code="MALFORMED_LOCK",
                    subject=f"{label}:{kind}",
                    file="package-lock.json",
                    message=str(exc),
                )
                continue

            for name in sorted(set(manifest_deps) - set(lock_deps)):
                self.error(
                    code="DEP_NOT_IN_LOCK",
                    subject=f"{label}>{name}",
                    file="package.json",
                    message=(
                        f"{kind} dependency {name!r} ({manifest_deps[name]}) is "
                        "declared but absent from the lock"
                    ),
                    chain_subject=owner_key,
                    expected=f"{name}: {manifest_deps[name]}",
                    actual=None,
                    diff_kind="presence",
                )
            for name in sorted(set(lock_deps) - set(manifest_deps)):
                self.error(
                    code="DEP_NOT_IN_MANIFEST",
                    subject=f"{label}>{name}",
                    file="package-lock.json",
                    message=(
                        f"lock {kind} entry {name!r} ({lock_deps[name]}) has no "
                        "matching manifest declaration"
                    ),
                    chain_subject=owner_key,
                    expected=None,
                    actual=f"{name}: {lock_deps[name]}",
                    diff_kind="presence",
                )
            for name in sorted(set(manifest_deps) & set(lock_deps)):
                if manifest_deps[name] != lock_deps[name]:
                    self.error(
                        code="LOCK_SPEC_DRIFT",
                        subject=f"{label}>{name}",
                        file="package-lock.json",
                        message=(
                            f"declared spec for {name!r} drifted: manifest says "
                            f"{manifest_deps[name]!r}, lock records "
                            f"{lock_deps[name]!r}"
                        ),
                        chain_subject=owner_key,
                        expected=manifest_deps[name],
                        actual=lock_deps[name],
                        diff_kind="value",
                    )

    def _reject_overrides_and_bad_specs(
        self, owner_key: str, manifest: dict[str, Any]
    ) -> None:
        if "overrides" in manifest:
            self.error(
                code="UNSUPPORTED_OVERRIDES",
                subject=owner_key or "<root>",
                file="package.json",
                message=(
                    "manifest 'overrides' is not supported by this gate; it "
                    "changes resolution non-locally and cannot be validated here"
                ),
            )

        kinds = (
            "dependencies",
            "optionalDependencies",
            "peerDependencies",
            "devDependencies",
        )
        for kind in kinds:
            deps = manifest.get(kind)
            if not isinstance(deps, dict):
                continue
            for name, spec in deps.items():
                if not isinstance(spec, str):
                    continue
                if spec.startswith("workspace:"):
                    # Valid only when it resolves to a workspace in this bundle.
                    self._validate_workspace_spec(owner_key, name, spec)
                    continue
                try:
                    parse_range(spec)
                except UnsupportedRangeError as exc:
                    self.error(
                        code="UNSUPPORTED_RANGE",
                        subject=f"{owner_key or '<root>'}>{name}",
                        file=(
                            "package.json"
                            if owner_key == ""
                            else f"{owner_key}/package.json"
                        ),
                        message=str(exc),
                        chain_subject=owner_key,
                    )
                except InvalidVersionError as exc:
                    self.error(
                        code="INVALID_VERSION_RANGE",
                        subject=f"{owner_key or '<root>'}>{name}",
                        file=(
                            "package.json"
                            if owner_key == ""
                            else f"{owner_key}/package.json"
                        ),
                        message=f"range for {name!r} is invalid: {exc}",
                        chain_subject=owner_key,
                    )

    def _validate_workspace_spec(
        self, owner_key: str, name: str, spec: str
    ) -> None:
        target = self.ws_by_name.get(name)
        if target is None:
            self.error(
                code="WORKSPACE_SPEC_UNRESOLVED",
                subject=f"{owner_key or '<root>'}>{name}",
                file="package.json" if owner_key == "" else f"{owner_key}/package.json",
                message=(
                    f"{spec!r} requires a local workspace named {name!r}, but no "
                    "workspace in this bundle provides it"
                ),
                chain_subject=owner_key,
            )
            return
        suffix = spec[len("workspace:") :]
        if suffix in ("*", "^", "~"):
            return
        if suffix.startswith(("^", "~", ">=", "<=", ">", "<", "=")) or suffix[0].isdigit():
            try:
                if target.version is None or not parse_range(suffix).satisfies(
                    Version.parse(target.version)
                ):
                    self.error(
                        code="WORKSPACE_SPEC_MISMATCH",
                        subject=f"{owner_key or '<root>'}>{name}",
                        file=(
                            "package.json"
                            if owner_key == ""
                            else f"{owner_key}/package.json"
                        ),
                        message=(
                            f"workspace range {spec!r} is not satisfied by "
                            f"{name}@{target.version}"
                        ),
                        expected=spec,
                        actual=f"{name}@{target.version}",
                        diff_kind="range",
                        chain_subject=owner_key,
                    )
            except (UnsupportedRangeError, InvalidVersionError) as exc:
                self.error(
                    code="UNSUPPORTED_RANGE",
                    subject=f"{owner_key or '<root>'}>{name}",
                    message=str(exc),
                    chain_subject=owner_key,
                )
        else:
            self.error(
                code="UNSUPPORTED_RANGE",
                subject=f"{owner_key or '<root>'}>{name}",
                message=f"workspace spec {spec!r} is not a recognized form",
                chain_subject=owner_key,
            )

    # -- graph traversal -----------------------------------------------------

    def _resolve(self, owner_key: str, dep_name: str) -> str | None:
        """npm node_modules walk: nearest ancestor node_modules wins."""
        if owner_key == "":
            ancestors = [""]
        else:
            ancestors = []
            parts = owner_key.split("/")
            for i in range(len(parts), 0, -1):
                ancestors.append("/".join(parts[:i]))
            ancestors.append("")
        tried: set[str] = set()
        for directory in ancestors:
            base = (
                f"node_modules/{dep_name}"
                if directory == ""
                else f"{directory}/node_modules/{dep_name}"
            )
            if base in tried:
                continue
            tried.add(base)
            if base in self.nodes:
                return base
        return None

    def _traverse_graph(self) -> None:
        # Multi-source DFS from every importer (root + workspaces).
        stack: list[tuple[str, str, str, str, bool, bool]] = []
        # seed importers
        for owner_key, obj in self._importer_seeds():
            for dep_name, spec, kind, edge_optional in self._edges_for(
                owner_key, obj
            ):
                stack.append((owner_key, dep_name, spec, kind, edge_optional, False))
            self.visited.add(owner_key)

        while stack:
            owner, dep_name, spec, kind, edge_optional, optional_ctx = stack.pop()
            target = self._resolve(owner, dep_name)
            edge = Edge(
                owner=owner,
                dep_name=dep_name,
                spec=spec,
                kind=kind,
                edge_optional=edge_optional,
                target=target,
            )
            self.edges_out[owner].append(edge)
            self.edge_index[(owner, dep_name)] = edge

            if target is None:
                if edge_optional:                    self.info(
                        code="OPTIONAL_DEP_MISSING",
                        subject=f"{owner}>{dep_name}",
                        message=(
                            f"optional dependency {dep_name!r} ({spec}) is not "
                            "present in the lock for this platform"
                        ),
                        chain_subject=owner,
                        note="optional; not required for the selected platform",
                    )
                else:
                    self.error(
                        code="MISSING_LOCK_ENTRY",
                        subject=f"{owner}>{dep_name}",
                        file="package-lock.json",
                        message=(
                            f"dependency {dep_name!r} ({spec}) required by "
                            f"{owner or '<root>'} has no resolvable lock entry"
                        ),
                        chain_subject=owner,
                        expected=f"node_modules/{dep_name}",
                        actual=None,
                        diff_kind="presence",
                    )
                continue

            self.reached.add(target)
            node = self.nodes[target]
            if node.is_link:
                link_target_key = self._resolve_link_target(target, node)
                self._validate_link(node, spec, link_target_key)
                if link_target_key and link_target_key in self.nodes:
                    target_node_key = link_target_key
                else:
                    continue
            else:
                target_node_key = target

            applies, reason = self._platform_applies(node)
            if not applies:
                if edge_optional or optional_ctx or node.optional:
                    self.info(
                        code="PLATFORM_EXCLUDED_OPTIONAL",
                        subject=target,
                        message=(
                            f"{dep_name!r} is excluded on "
                            f"{self.platform.os}/{self.platform.cpu}"
                            f" ({reason}); skipped because it is optional"
                        ),
                        chain_subject=target,
                        note=reason,
                    )
                    continue
                self.error(
                    code="PLATFORM_MISMATCH",
                    subject=target,
                    file="package-lock.json",
                    message=(
                        f"required dependency {dep_name!r} does not support "
                        f"{self.platform.os}/{self.platform.cpu}: {reason}"
                    ),
                    chain_subject=target,
                    expected="platform matching current os/cpu/libc",
                    actual=reason,
                    diff_kind="value",
                )
                continue

            # Range/version agreement — links point at workspaces whose spec is
            # validated separately; registry specs must satisfy node version.
            if not node.is_link and not node.is_workspace:
                self._check_version_edge(node, dep_name, spec, owner)
                self._verify_artifact(node)
            elif node.is_link and link_target_key in self.workspaces:
                ws = self.workspaces[link_target_key]
                if not spec.startswith("workspace:"):
                    self.error(
                        code="WORKSPACE_LINK_SPEC",
                        subject=target,
                        file="package-lock.json",
                        message=(
                            f"link to workspace {ws.name!r} must use a "
                            f"workspace: spec, found {spec!r}"
                        ),
                        chain_subject=target,
                    )

            if target_node_key in self.visited:
                continue
            self.visited.add(target_node_key)

            node_obj = self.nodes[target_node_key]
            next_optional_ctx = optional_ctx or edge_optional or node_obj.optional
            for n_name, n_spec, n_kind, n_opt in self._edges_for(
                target_node_key, node_obj.data
            ):
                stack.append(
                    (
                        target_node_key,
                        n_name,
                        n_spec,
                        n_kind,
                        n_opt,
                        next_optional_ctx,
                    )
                )

    def _importer_seeds(self) -> list[tuple[str, dict[str, Any]]]:
        seeds: list[tuple[str, dict[str, Any]]] = []
        root = self.nodes.get("")
        if root is not None:
            seeds.append(("", root.data))
        for directory, ws in sorted(self.workspaces.items()):
            node = self.nodes.get(directory)
            if node is not None:
                seeds.append((directory, node.data))
        return seeds

    def _edges_for(
        self, owner_key: str, data: dict[str, Any]
    ) -> list[tuple[str, str, str, bool]]:
        """Return (name, spec, kind, edge_optional) in deterministic order."""
        is_importer = owner_key == "" or owner_key in self.workspaces
        kinds = list(IMPORTER_KINDS) if is_importer else list(DEP_KINDS)
        if self.production and owner_key in self._importer_seed_keys():
            kinds = [k for k in kinds if k != "devDependencies"]
        edges: list[tuple[str, str, str, bool]] = []
        peer_meta = data.get("peerDependenciesMeta")
        optional_peers: set[str] = set()
        if isinstance(peer_meta, dict):
            for name, meta in peer_meta.items():
                if isinstance(meta, dict) and meta.get("optional") is True:
                    optional_peers.add(name)
        for kind in kinds:
            deps = data.get(kind)
            if not isinstance(deps, dict):
                continue
            for name in sorted(deps):
                spec = deps[name]
                if not isinstance(spec, str):
                    continue
                edge_optional = kind == "optionalDependencies" or (
                    kind == "peerDependencies" and name in optional_peers
                )
                edges.append((name, spec, kind, edge_optional))
        return edges

    def _importer_seed_keys(self) -> set[str]:
        return set(self.workspaces) | {""}

    def _resolve_link_target(self, link_key: str, node: LockNode) -> str | None:
        """Resolve a v3 link's ``resolved`` path to a lock key.

        npm lockfile v3 records workspace links with a ``resolved`` path
        relative to the *project root* (e.g. ``packages/tools`` for a link at
        ``node_modules/tools``). Anything that escapes the root via ``..`` or
        is absolute is refused.
        """
        raw = node.link_target
        if not raw:
            return None
        joined = posixpath.normpath(raw)
        if joined.startswith("..") or joined.startswith("/") or joined == ".":
            self.error(
                code="UNSUPPORTED_LINK_TARGET",
                subject=link_key,
                file="package-lock.json",
                message=(
                    f"link target {raw!r} resolves outside the project; refused"
                ),
                chain_subject=link_key,
            )
            return None
        return joined

    def _validate_link(self, node: LockNode, spec: str, target_key: str | None) -> None:
        if not target_key:
            self.error(
                code="MALFORMED_LINK",
                subject=node.key,
                file="package-lock.json",
                message="link node has no resolvable workspace target",
            )
            return
        if target_key not in self.nodes:
            self.error(
                code="BROKEN_WORKSPACE_LINK",
                subject=node.key,
                file="package-lock.json",
                message=(
                    f"link target {node.link_target!r} ({target_key!r}) does not "
                    "exist in the lock"
                ),
                chain_subject=node.key,
            )
            return
        target_node = self.nodes[target_key]
        if not target_node.is_workspace:
            self.error(
                code="UNSUPPORTED_LINK_TARGET",
                subject=node.key,
                file="package-lock.json",
                message=(
                    "links may only point at local workspaces; link to "
                    f"{target_key!r} is refused"
                ),
                chain_subject=node.key,
            )

    def _platform_applies(self, node: LockNode) -> tuple[bool, str]:
        for label, declared, current in (
            ("os", node.os, self.platform.os),
            ("cpu", node.cpu, self.platform.cpu),
            ("libc", node.libc, self.platform.libc),
        ):
            if not declared:
                continue
            if current is None and label == "libc":
                continue
            positives = [v for v in declared if not v.startswith("!")]
            negatives = [v[1:] for v in declared if v.startswith("!")]
            if positives and current not in positives:
                return False, f"requires {label} in {positives}, current is {current!r}"
            if current in negatives:
                return False, f"forbidden by {label}!{current}"
        return True, "applies"

    def _check_version_edge(
        self, node: LockNode, dep_name: str, spec: str, owner: str
    ) -> None:
        if spec.startswith("workspace:"):
            self.error(
                code="WORKSPACE_SPEC_TO_REGISTRY",
                subject=node.key,
                file="package-lock.json",
                message=(
                    f"resolved node for {dep_name!r} is a registry package but "
                    f"the spec {spec!r} requires a workspace"
                ),
                chain_subject=node.key,
            )
            return
        if node.in_bundle:
            return  # bundled copies carry no registry version obligations
        if node.version is None:
            self.error(
                code="MISSING_NODE_VERSION",
                subject=node.key,
                file="package-lock.json",
                message="registry node has no version string",
                chain_subject=node.key,
            )
            return
        try:
            version = Version.parse(node.version)
        except InvalidVersionError:
            self.error(
                code="INVALID_NODE_VERSION",
                subject=node.key,
                file="package-lock.json",
                message=f"node version {node.version!r} is not valid semver",
                chain_subject=node.key,
                expected="X.Y.Z semver",
                actual=node.version,
                diff_kind="value",
            )
            return
        try:
            dep_range = parse_range(spec)
        except UnsupportedRangeError as exc:
            self.error(
                code="UNSUPPORTED_RANGE",
                subject=f"{owner}>{dep_name}",
                file="package-lock.json",
                message=str(exc),
                chain_subject=node.key,
            )
            return
        if not dep_range.satisfies(version):
            self.error(
                code="LOCK_RANGE_DRIFT",
                subject=node.key,
                file="package-lock.json",
                message=(
                    f"locked {dep_name}@{node.version} does not satisfy the "
                    f"declared range {spec!r} required from {owner or '<root>'}"
                ),
                expected=spec,
                actual=f"{dep_name}@{node.version}",
                diff_kind="range",
                chain_subject=node.key,
            )

    # -- artifact verification ----------------------------------------------

    def _vendor_path_for(self, node: LockNode) -> str | None:
        parsed = urlparse(node.resolved or "")
        if parsed.scheme == "https" and parsed.netloc and parsed.path:
            exact = f"vendor/{parsed.netloc}{parsed.path}"
            if exact in self.files:
                return exact
        basename = posixpath.basename(parsed.path or "")
        if basename:
            matches = [
                p
                for p in self.files
                if p.startswith("vendor/") and posixpath.basename(p) == basename
            ]
            if len(matches) == 1:
                return matches[0]
        return None

    def _verify_artifact(self, node: LockNode) -> None:
        if node.in_bundle:
            return  # bundled dependencies ship inside their parent tarball
        if not node.integrity:
            self.error(
                code="MISSING_INTEGRITY",
                subject=node.key,
                file="package-lock.json",
                message=(
                    f"{node.name}@{node.version} has no integrity field; its "
                    "tarball cannot be authenticated — a lock file alone is not "
                    "proof of reproducibility"
                ),
                chain_subject=node.key,
            )
        else:
            try:
                parsed_integrity = parse_integrity(node.integrity)
            except IntegrityError as exc:
                self.error(
                    code="INVALID_INTEGRITY",
                    subject=node.key,
                    file="package-lock.json",
                    message=str(exc),
                    chain_subject=node.key,
                )
                parsed_integrity = None
            if parsed_integrity is not None and parsed_integrity.is_weak:
                self.weak_algorithms += 1
                self.warn(
                    code="WEAK_INTEGRITY",
                    subject=node.key,
                    file="package-lock.json",
                    message=(
                        f"{node.name}@{node.version} is pinned with "
                        f"{parsed_integrity.algorithm}; collision attacks make "
                        "this unsuitable for a reproducibility gate"
                    ),
                    chain_subject=node.key,
                )

        vendor_path = self._vendor_path_for(node)
        if vendor_path is None:
            self.missing_tarballs += 1
            message = (
                f"{node.name}@{node.version} tarball is not vendored; the lock "
                "references it but nothing in this bundle proves its bytes. "
                "Having package-lock.json is therefore NOT sufficient for "
                "offline reproducibility"
            )
            if self.require_vendored_tarballs:
                self.error(
                    code="TARBALL_NOT_VENDORED",
                    subject=node.key,
                    message=message,
                    chain_subject=node.key,
                    note=f"expected under vendor/ from {node.resolved}",
                )
            else:
                self.info(
                    code="TARBALL_NOT_VENDORED",
                    subject=node.key,
                    message=message,
                    chain_subject=node.key,
                    note=f"expected under vendor/ from {node.resolved}",
                )
            return

        blob = self.files[vendor_path]
        verified_ok = False
        if node.integrity:
            try:
                verify_tarball(node.integrity, blob)
                verified_ok = True
            except IntegrityError as exc:
                self.error(
                    code="TARBALL_INTEGRITY_MISMATCH",
                    subject=node.key,
                    file=vendor_path,
                    message=f"content hash mismatch for {vendor_path}: {exc}",
                    chain_subject=node.key,
                    expected=node.integrity,
                    actual="recomputed digest differed",
                    diff_kind="file",
                )
                return
        if verified_ok:
            self.verified_tarballs += 1
        try:
            manifest_info = inspect_tarball(blob)
        except IntegrityError as exc:
            self.error(
                code="TARBALL_CORRUPT",
                subject=node.key,
                file=vendor_path,
                message=str(exc),
                chain_subject=node.key,
            )
            return
        if manifest_info.name and manifest_info.name != node.name:
            self.error(
                code="TARBALL_MANIFEST_MISMATCH",
                subject=node.key,
                file=vendor_path,
                message=(
                    f"tarball identifies itself as {manifest_info.name!r}, lock "
                    f"claims {node.name!r}"
                ),
                chain_subject=node.key,
                expected=node.name,
                actual=manifest_info.name,
                diff_kind="value",
            )
        if manifest_info.version and node.version and manifest_info.version != node.version:
            self.error(
                code="TARBALL_MANIFEST_MISMATCH",
                subject=node.key,
                file=vendor_path,
                message=(
                    f"tarball contains version {manifest_info.version!r}, lock "
                    f"claims {node.version!r}"
                ),
                chain_subject=node.key,
                expected=node.version,
                actual=manifest_info.version,
                diff_kind="value",
            )
        if manifest_info.has_install_scripts or node.has_install_script:
            hooks = ", ".join(sorted(manifest_info.scripts)) or "lock-flagged"
            self.warn(
                code="INSTALL_SCRIPT_PRESENT",
                subject=node.key,
                file=vendor_path,
                message=(
                    f"{node.name}@{node.version} ships install lifecycle hooks "
                    f"({hooks}); the gate NEVER executes them — review is "
                    "required before trusting an install"
                ),
                chain_subject=node.key,
                note="scripts are reported, not run",
            )

    # -- whole-graph checks --------------------------------------------------

    def _check_extraneous(self) -> None:
        for key, node in self.nodes.items():
            if key == "" or node.is_workspace or node.is_link:
                continue
            if node.in_bundle:
                continue
            if key not in self.visited and key not in self.reached:
                self.warn(
                    code="EXTRANEOUS_LOCK_NODE",
                    subject=key,
                    file="package-lock.json",
                    message=(
                        f"{node.name}@{node.version} is pinned in the lock but "
                        "unreachable from any manifest dependency"
                    ),
                )

    def _check_duplicates(self) -> None:
        groups: dict[str, list[LockNode]] = defaultdict(list)
        for key, node in self.nodes.items():
            if (
                key == ""
                or node.is_link
                or node.is_workspace
                or node.in_bundle
                or key not in self.visited
            ):
                continue
            groups[node.name].append(node)
        for name, members in sorted(groups.items()):
            by_version: dict[str, list[LockNode]] = defaultdict(list)
            for node in members:
                by_version[node.version or "<none>"].append(node)
            for version, copies in sorted(by_version.items()):
                if len(copies) < 2:
                    continue
                identities = {(c.integrity, c.resolved) for c in copies}
                keys = ", ".join(c.key for c in copies)
                if len(identities) > 1:
                    self.error(
                        code="CONFLICTING_DUPLICATES",
                        subject=name,
                        file="package-lock.json",
                        message=(
                            f"{name}@{version} is locked {len(copies)} times with "
                            "different integrity/resolved values; resolution is "
                            f"ambiguous. Keys: {keys}"
                        ),
                        expected="identical integrity for identical versions",
                        actual="; ".join(sorted({str(i[0]) for i in identities})),
                        diff_kind="value",
                    )
                else:
                    self.warn(
                        code="DUPLICATE_VERSION_COPIES",
                        subject=name,
                        file="package-lock.json",
                        message=(
                            f"{name}@{version} occurs {len(copies)} times with "
                            f"identical content ({keys}); redundant but consistent"
                        ),
                    )
            distinct = sorted(v for v in by_version if v != "<none>")
            if len(distinct) > 1:
                self.info(
                    code="MULTIPLE_VERSIONS",
                    subject=name,
                    file="package-lock.json",
                    message=(
                        f"{name} resolves to {len(distinct)} distinct versions: "
                        + ", ".join(distinct)
                    ),
                )

    def _check_cycles(self) -> None:
        # Collapse links to their targets and keep registry edges only.
        graph: dict[str, set[str]] = defaultdict(set)
        for owner, edges in self.edges_out.items():
            for edge in edges:
                if not edge.target:
                    continue
                node = self.nodes.get(edge.target)
                if node is None:
                    continue
                real = node.link_target if node.is_link else edge.target
                if real and real in self.visited:
                    graph[owner].add(real)

        index_counter = [0]
        stack: list[str] = []
        lowlink: dict[str, int] = {}
        index: dict[str, int] = {}
        on_stack: dict[str, bool] = {}
        components: list[list[str]] = []

        def strongconnect(v: str) -> None:
            index[v] = index_counter[0]
            lowlink[v] = index_counter[0]
            index_counter[0] += 1
            stack.append(v)
            on_stack[v] = True
            for w in graph.get(v, ()):  # type: ignore[arg-type]
                if w not in index:
                    strongconnect(w)
                    lowlink[v] = min(lowlink[v], lowlink[w])
                elif on_stack.get(w):
                    lowlink[v] = min(lowlink[v], index[w])
            if lowlink[v] == index[v]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    comp.append(w)
                    if w == v:
                        break
                components.append(comp)

        import sys

        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(old_limit, 10_000))
        try:
            for vertex in list(graph):
                if vertex not in index:
                    strongconnect(vertex)
        finally:
            sys.setrecursionlimit(old_limit)

        for comp in components:
            cycle_keys = self._cycle_chain(comp, graph)
            if not cycle_keys:
                continue
            labels = [self._node_label(k) for k in cycle_keys]
            self.warn(
                code="DEPENDENCY_CYCLE",
                subject=cycle_keys[0],
                file="package-lock.json",
                message=(
                    "circular dependency detected: " + " -> ".join(labels) + " -> "
                    f"{self._node_label(cycle_keys[0])}"
                ),
                chain_subject=cycle_keys[0],
            )

    def _cycle_chain(
        self, component: list[str], graph: dict[str, set[str]]
    ) -> list[str]:
        members = set(component)
        # self loop
        for v in component:
            if v in graph.get(v, set()):
                return [v]
        if len(component) < 2:
            return []
        # BFS from the component's lexicographically smallest key, staying
        # inside the SCC, to find the shortest path back.
        start = sorted(component)[0]
        prev: dict[str, str] = {}
        queue = deque([start])
        seen = {start}
        end = None
        while queue:
            cur = queue.popleft()
            for nxt in sorted(graph.get(cur, set())):
                if nxt not in members:
                    continue
                if nxt == start and cur != start:
                    end = cur
                    break
                if nxt not in seen:
                    seen.add(nxt)
                    prev[nxt] = cur
                    queue.append(nxt)
            if end:
                break
        if end is None:
            return []
        path = [end]
        while path[-1] != start:
            path.append(prev[path[-1]])
        path.reverse()
        return path

    # -- chains and report assembly -----------------------------------------

    def _node_label(self, key: str) -> str:
        if key == "":
            return "<root>"
        node = self.nodes.get(key)
        if node and node.is_workspace:
            return f"workspace:{key}"
        if node and node.is_link:
            return f"link:{node.name}"
        if node:
            return f"{node.name}@{node.version or '?'}"
        return key

    def _shortest_chain(self, subject_key: str | None) -> list[str]:
        if subject_key is None:
            return []
        if subject_key in self._importer_seed_keys():
            return [self._node_label(subject_key)]
        # Build a resolved graph (links collapse).
        adjacency: dict[str, list[tuple[str, Edge]]] = defaultdict(list)
        for owner, edges in self.edges_out.items():
            for edge in edges:
                if not edge.target:
                    continue
                target_node = self.nodes.get(edge.target)
                real = target_node.link_target if target_node and target_node.is_link else edge.target
                adjacency[owner].append((real, edge))
        sources = sorted(self._importer_seed_keys())
        prev: dict[str, tuple[str, Edge]] = {}
        queue = deque(sources)
        seen = set(sources)
        found = False
        while queue and not found:
            cur = queue.popleft()
            for nxt, edge in adjacency.get(cur, []):
                if nxt in seen:
                    continue
                seen.add(nxt)
                prev[nxt] = (cur, edge)
                if nxt == subject_key:
                    found = True
                    break
                queue.append(nxt)
        if subject_key not in seen:
            return [self._node_label(subject_key)]
        path_keys: list[str] = []
        node = subject_key
        while node not in sources:
            path_keys.append(node)
            node, _edge = prev[node]
        path_keys.reverse()
        chain = [self._node_label(node)]
        cursor = node
        for key in path_keys:
            _, edge = prev[key]
            target_node = self.nodes.get(key)
            label = self._node_label(key)
            chain.append(f"--{edge.dep_name} {edge.spec!r}--> {label}")
            cursor = key
        return chain

    def _build_report(self, lock: dict[str, Any]) -> dict[str, Any]:
        # attach chains
        enriched: list[Finding] = []
        for finding in self.findings:
            subject = finding.chain_subject
            if subject is None and finding.subject.startswith("node_modules/"):
                subject = finding.subject
            chain = self._shortest_chain(subject)
            enriched.append((finding, chain))

        order = {"error": 0, "warning": 1, "info": 2}
        enriched.sort(key=lambda fc: (order[fc[0].severity], fc[0].code, fc[0].subject))

        findings_out = []
        errors = warnings = infos = 0
        reproducibility_reasons: list[str] = []
        for finding, chain in enriched:
            if finding.severity == "error":
                errors += 1
            elif finding.severity == "warning":
                warnings += 1
                if finding.code in {"INSTALL_SCRIPT_PRESENT", "WEAK_INTEGRITY"}:
                    reproducibility_reasons.append(
                        f"{finding.code}: {finding.subject}"
                    )
            else:
                infos += 1
                if finding.code == "TARBALL_NOT_VENDORED":
                    reproducibility_reasons.append(
                        f"missing vendored artifact: {finding.subject}"
                    )
            diff = None
            if finding.diff_kind:
                unified = None
                if finding.diff_kind in ("value", "range"):
                    unified = _unified(
                        finding.expected,
                        finding.actual,
                        "expected (manifest/stated)",
                        "actual (lock/content)",
                    )
                diff = {
                    "kind": finding.diff_kind,
                    "expected": finding.expected,
                    "actual": finding.actual,
                    "unified": unified,
                    "note": finding.note,
                }
            findings_out.append(
                {
                    "code": finding.code,
                    "severity": finding.severity,
                    "subject": finding.subject,
                    "message": finding.message,
                    "chain": chain,
                    "file": finding.file,
                    "diff": diff,
                }
            )

        registry_nodes = sum(
            1
            for n in self.nodes.values()
            if n.key != "" and not n.is_link and not n.is_workspace and not n.in_bundle
        )
        reproducible = (
            errors == 0
            and self.missing_tarballs == 0
            and self.weak_algorithms == 0
            and not any(f.code == "INSTALL_SCRIPT_PRESENT" for f, _ in enriched)
        )
        if not reproducible and not reproducibility_reasons:
            reproducibility_reasons.append("one or more blocking findings above")

        return {
            "status": "fail" if errors else "pass",
            "lockfile_version": lock.get("lockfileVersion")
            if isinstance(lock.get("lockfileVersion"), int)
            else None,
            "package_manager": "npm",
            "platform": {
                "os": self.platform.os,
                "cpu": self.platform.cpu,
                "libc": self.platform.libc,
            },
            "production": self.production,
            "require_vendored_tarballs": self.require_vendored_tarballs,
            "summary": {
                "total_lock_nodes": max(len(self.nodes) - 1, 0),
                "visited_nodes": len(self.visited - self._importer_seed_keys()),
                "registry_nodes": registry_nodes,
                "workspace_nodes": len(self.workspaces),
                "link_nodes": sum(1 for n in self.nodes.values() if n.is_link),
                "vendored_tarballs_verified": self.verified_tarballs,
                "tarballs_missing": self.missing_tarballs,
                "weak_integrity_algorithms": self.weak_algorithms,
                "errors": errors,
                "warnings": warnings,
                "infos": infos,
            },
            "reproducibility": {
                "status": reproducible,
                "reasons": reproducibility_reasons if not reproducible else [],
            },
            "findings": findings_out,
        }


def verify_bundle(
    files: dict[str, bytes],
    *,
    os_name: str = "linux",
    cpu: str = "x64",
    libc: str | None = "glibc",
    production: bool = False,
    require_vendored_tarballs: bool = False,
) -> dict[str, Any]:
    """Convenience entry point used by both the API and the CLI."""
    platform = _Platform(os=os_name, cpu=cpu, libc=libc)
    verifier = Verifier(
        files,
        platform,
        production=production,
        require_vendored_tarballs=require_vendored_tarballs,
    )
    return verifier.verify()
