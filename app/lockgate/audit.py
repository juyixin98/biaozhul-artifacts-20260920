"""Dependency-lock consistency gate.

The auditor compares a Node project's ``package.json`` against a v3
``package-lock.json`` *and* against actual artifact bytes when they are
supplied. It performs real work — semver evaluation, graph resolution,
cycle detection, cryptographic digest recomputation — and reports the
shortest dependency chain that explains each finding.

Design constraints:

* lockfile v3 only; every other version is an explicit unsupported-syntax
  rejection (v1/v2 upgrades must be deliberate);
* registry tarballs only — ``file:``, ``git:``, aliases, tags, URLs are
  unsupported syntax and listed verbatim;
* the lockfile merely existing is never treated as reproducibility: every
  node must be *pinned* (resolved+integrity) and, when artifacts are
  shipped, *verified* byte-for-byte;
* nothing in the archive is executed or extracted to disk.
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .archive import Archive
from .diffing import json_diff
from .integrity import IntegrityError, compute_digest, parse_integrity, verify
from .platforms import Target, evaluate as platform_evaluate
from .semver import UnsupportedSpecError, parse_version, satisfies
from .workspace_glob import UnsupportedGlobError, expand as glob_expand

SUPPORTED_LOCKFILE_VERSION = 3
ROOT_LOCK_KEY = ""
# Edges are stored as (name, optional:bool, dev:bool, via_declared:bool)


@dataclass
class LockNode:
    key: str                      # path key under packages/, e.g. "node_modules/ab"
    name: str                     # real package name
    version: Optional[str]
    resolved: Optional[str]
    integrity: Optional[str]
    raw: dict
    deps: Dict[str, str] = field(default_factory=dict)
    optional_deps: Dict[str, str] = field(default_factory=dict)
    dev_deps: Dict[str, str] = field(default_factory=dict)
    peer_deps: Dict[str, str] = field(default_factory=dict)
    link: bool = False
    link_target: Optional[str] = None
    optional: bool = False
    dev: bool = False
    dev_only: bool = False
    extraneous: bool = False
    engines: dict = field(default_factory=dict)
    os: List[str] = field(default_factory=list)
    cpu: List[str] = field(default_factory=list)
    libc: List[str] = field(default_factory=list)
    bin: dict = field(default_factory=dict)
    scripts: dict = field(default_factory=dict)
    has_install_script: bool = False


@dataclass
class _FindingBuilder:
    items: List[dict] = field(default_factory=list)
    unsupported: List[str] = field(default_factory=list)

    def add(self, code, severity, location, message, *, chain=None,
            diff=None, evidence=None) -> None:
        self.items.append({
            "code": code,
            "severity": severity,
            "location": location,
            "message": message,
            "chain": chain or [],
            "diff": diff,
            "evidence": evidence or {},
        })

    def unsupported_syntax(self, location, spec, reason) -> None:
        token = f"{location}: {spec!r} ({reason})"
        if token not in self.unsupported:
            self.unsupported.append(token)
        self.add("unsupported-syntax", "error", location,
                 f"unsupported dependency syntax {spec!r}: {reason}",
                 evidence={"spec": spec})


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _load_json(archive: Archive, candidates: List[str], what: str):
    found = None
    for path in candidates:
        data = archive.get(path)
        if data is not None:
            found = (path, data)
            break
    if found is None:
        raise FileNotFoundError(what)
    path, data = found
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not valid UTF-8: {exc}") from exc
    try:
        return path, json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc


def load_bundle(archive: Archive, *, package_path: Optional[str] = None,
                lock_path: Optional[str] = None) -> Tuple[str, dict, str, dict, List[str]]:
    """Locate package.json/package-lock.json, possibly in a sub-directory."""
    pkg_candidates = [package_path] if package_path else [
        "package.json",
    ]
    lock_candidates = [lock_path] if lock_path else [
        "package-lock.json",
        "npm-shrinkwrap.json",
    ]
    pkg_path, pkg = _load_json(archive, [c for c in pkg_candidates if c], "package.json")
    lock_path_found, lock = _load_json(archive, [c for c in lock_candidates if c], "package-lock.json")
    if not isinstance(pkg, dict):
        raise ValueError(f"{pkg_path} must be a JSON object")
    if not isinstance(lock, dict):
        raise ValueError(f"{lock_path_found} must be a JSON object")
    audited = [pkg_path, lock_path_found]
    return pkg_path, pkg, lock_path_found, lock, audited


def _is_workspace_target_key(key: str) -> bool:
    """v3 keys that are physical workspace packages, e.g. 'packages/lib'."""
    return key != ROOT_LOCK_KEY and not key.startswith("node_modules/") and "/node_modules/" not in key


# ---------------------------------------------------------------------------
# Lock node model
# ---------------------------------------------------------------------------

def _build_lock_nodes(lock: dict, fb: _FindingBuilder) -> Dict[str, LockNode]:
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        fb.add("lock-structure", "error", "package-lock.json",
               "lockfile has no top-level 'packages' object (v3 layout required)")
        return {}
    nodes: Dict[str, LockNode] = {}
    for key, entry in packages.items():
        if not isinstance(entry, dict):
            fb.add("lock-structure", "error", f"packages[{key!r}]",
                   "package entry must be an object")
            continue
        if key == ROOT_LOCK_KEY:
            name = entry.get("name", "") or ""
        else:
            # v3 keys look like node_modules/foo or node_modules/@scope/bar
            base = key.split("node_modules/")[-1]
            name = entry.get("name") or base
        node = LockNode(
            key=key,
            name=name,
            version=entry.get("version"),
            resolved=entry.get("resolved"),
            integrity=entry.get("integrity"),
            raw=entry,
            deps=dict(entry.get("dependencies") or {}),
            optional_deps=dict(entry.get("optionalDependencies") or {}),
            dev_deps=dict(entry.get("devDependencies") or {}),
            peer_deps=dict(entry.get("peerDependencies") or {}),
            link=bool(entry.get("link")),
            link_target=entry.get("resolved") if entry.get("link") else None,
            optional=bool(entry.get("optional")),
            dev=bool(entry.get("dev")),
            dev_only=bool(entry.get("dev")) and not entry.get("inBundle"),
            extraneous=bool(entry.get("extraneous")),
            engines=dict(entry.get("engines") or {}),
            os=list(entry.get("os") or []),
            cpu=list(entry.get("cpu") or []),
            libc=list(entry.get("libc") or []),
            bin=dict(entry.get("bin") or {}) if isinstance(entry.get("bin", {}), dict) else {},
            scripts=dict(entry.get("scripts") or {}) if isinstance(entry.get("scripts", {}), dict) else {},
        )
        node.has_install_script = bool(entry.get("hasInstallScript")) or any(
            k in node.scripts for k in ("preinstall", "install", "postinstall")
        )
        nodes[key] = node
    return nodes


# ---------------------------------------------------------------------------
# Resolution graph (node_modules nesting as encoded by v3 keys)
# ---------------------------------------------------------------------------

def _resolve_key(nodes: Dict[str, LockNode], owner_key: str, dep_name: str) -> Optional[str]:
    """Mimic Node resolution: walk upward from owner through node_modules."""
    # owner_key examples: "", "node_modules/a", "node_modules/a/node_modules/b"
    prefixes = []
    if owner_key:
        prefixes.append(owner_key + "/node_modules/" + dep_name)
        parts = owner_key.split("/node_modules/")
        # every ancestor node_modules
        for i in range(len(parts) - 1, 0, -1):
            ancestor = "/node_modules/".join(parts[:i])
            prefixes.append(ancestor + "/node_modules/" + dep_name)
    prefixes.append("node_modules/" + dep_name)
    for cand in prefixes:
        if cand in nodes:
            return cand
    return None


def _parent_tree(nodes: Dict[str, LockNode]) -> Tuple[Dict[str, Set[str]], Dict[str, bool]]:
    """Build reverse edges: child -> set of parent keys, plus edge optional flag."""
    parents: Dict[str, Set[str]] = defaultdict(set)
    edge_optional: Dict[Tuple[str, str], bool] = {}

    def add(parent_key: str, child_name: str, optional: bool) -> Optional[str]:
        child_key = _resolve_key(nodes, parent_key, child_name)
        if child_key is None:
            return None
        parents[child_key].add(parent_key)
        prev = edge_optional.get((parent_key, child_key))
        # an edge is required only when ALL declarations say required
        edge_optional[(parent_key, child_key)] = optional if prev is None else (prev and optional)
        return child_key

    for key, node in nodes.items():
        if key == ROOT_LOCK_KEY:
            continue
        for d in node.deps:
            add(key, d, False)
        for d in node.optional_deps:
            add(key, d, True)
    # root edges
    root = nodes.get(ROOT_LOCK_KEY)
    if root:
        for d in root.deps:
            add(ROOT_LOCK_KEY, d, False)
        for d in root.optional_deps:
            add(ROOT_LOCK_KEY, d, True)
        for d in root.dev_deps:
            add(ROOT_LOCK_KEY, d, False)
    return parents, edge_optional


def shortest_chains(nodes: Dict[str, LockNode]) -> Dict[str, List[str]]:
    """BFS from root over lock-declared edges => shortest chain per node."""
    # forward adjacency from both packages[].dependencies and root
    forward: Dict[str, List[str]] = defaultdict(list)
    seen_edges: Set[Tuple[str, str]] = set()

    def edge(a: str, b: str):
        if (a, b) not in seen_edges:
            seen_edges.add((a, b))
            forward[a].append(b)

    for key, node in nodes.items():
        if key == ROOT_LOCK_KEY:
            continue
        for d in list(node.deps) + list(node.optional_deps):
            ck = _resolve_key(nodes, key, d)
            if ck:
                edge(key, ck)
    root = nodes.get(ROOT_LOCK_KEY)
    if root:
        for d in list(root.deps) + list(root.optional_deps) + list(root.dev_deps):
            ck = _resolve_key(nodes, ROOT_LOCK_KEY, d)
            if ck:
                edge(ROOT_LOCK_KEY, ck)

    chains: Dict[str, List[str]] = {ROOT_LOCK_KEY: []}
    q = deque([ROOT_LOCK_KEY])
    for key in nodes:
        if _is_workspace_target_key(key):
            chains[key] = []
            q.append(key)
    while q:
        cur = q.popleft()
        for nxt in forward.get(cur, []):
            if nxt not in chains:
                chains[nxt] = chains[cur] + [cur]
                q.append(nxt)
    return chains


def label(nodes: Dict[str, LockNode], key: str) -> str:
    if key == ROOT_LOCK_KEY:
        root = nodes.get(key)
        return f"{root.name}@{root.version}" if root and root.name else "<root>"
    n = nodes[key]
    return f"{n.name}@{n.version or '?'}"


def find_cycles(nodes: Dict[str, LockNode]) -> List[Tuple[List[str], List[str]]]:
    """Tarjan SCC; return [(cycle_lock_keys, readable_labels)] per nontrivial SCC."""
    adj: Dict[str, List[str]] = defaultdict(list)
    for key, node in nodes.items():
        if key == ROOT_LOCK_KEY:
            continue
        for d in list(node.deps) + list(node.optional_deps):
            ck = _resolve_key(nodes, key, d)
            if ck and ck != ROOT_LOCK_KEY and not _is_workspace_target_key(ck):
                adj[key].append(ck)
    # physical workspace targets are not part of the node_modules cycle graph
    for k in list(adj):
        if _is_workspace_target_key(k):
            del adj[k]

    index = 0
    stack: List[str] = []
    on_stack: Set[str] = set()
    indices: Dict[str, int] = {}
    low: Dict[str, int] = {}
    sccs: List[List[str]] = []

    def strongconnect(v: str):
        nonlocal index
        indices[v] = low[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj.get(v, []):
            if w not in indices:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], indices[w])
        if low[v] == indices[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                comp.append(w)
                if w == v:
                    break
            sccs.append(comp)

    for key in list(nodes):
        if key != ROOT_LOCK_KEY and key not in indices:
            strongconnect(key)

    cycles: List[Tuple[List[str], List[str]]] = []
    for comp in sccs:
        members = set(comp)
        if len(comp) == 1:
            v = comp[0]
            if v not in adj.get(v, []):
                continue
        # find shortest cycle within the SCC by BFS from its lexicographically
        # smallest member; deterministic and easy to review.
        start = min(comp)
        prev: Dict[str, Optional[str]] = {start: None}
        q = deque([start])
        found = None
        while q and found is None:
            cur = q.popleft()
            for w in adj.get(cur, []):
                if w not in members:
                    continue
                if w == start and cur != start:
                    found = cur
                    break
                if w not in prev:
                    prev[w] = cur
                    q.append(w)
        if found is None and len(comp) == 1:
            cyc = [start]
        elif found is not None:
            path = []
            node = found
            while node is not None:
                path.append(node)
                node = prev[node]
            path.reverse()
            cyc = path
        else:
            cyc = sorted(comp)
        cycles.append((cyc, [label(nodes, k) for k in cyc]))
    cycles.sort(key=lambda pair: pair[1])
    return cycles


# ---------------------------------------------------------------------------
# URL / protocol checks
# ---------------------------------------------------------------------------

def _check_resolved(node: LockNode, allowed_registries: List[str], fb: _FindingBuilder,
                    chain: List[str], loc: str) -> bool:
    if node.link:
        return True
    if not node.resolved:
        fb.add("missing-resolved", "error", loc,
               f"{node.name}@{node.version} has no 'resolved' URL",
               chain=chain)
        return False
    from urllib.parse import urlparse
    u = urlparse(node.resolved)
    if u.scheme != "https":
        fb.add("non-https-resolved", "error", loc,
               f"{node.name}@{node.version} resolved over {u.scheme or 'missing'} scheme; https required",
               chain=chain, evidence={"resolved": node.resolved})
        return False
    host_ok = any(
        (u.hostname or "").lower() == reg.lower() or (u.hostname or "").lower().endswith("." + reg.lower())
        for reg in allowed_registries
    )
    if not host_ok:
        fb.add("untrusted-registry", "error", loc,
               f"{node.name}@{node.version} resolves to untrusted host {u.hostname!r}",
               chain=chain, evidence={"resolved": node.resolved,
                                      "allowed_registries": allowed_registries})
        return False
    if not u.path.endswith((".tgz", ".tar.gz")):
        fb.add("resolved-not-tarball", "error", loc,
               f"{node.name}@{node.version} resolved URL does not point at a .tgz artifact",
               chain=chain, evidence={"resolved": node.resolved})
        return False
    return True


def _check_spec(node_name: str, spec: str, location: str, fb: _FindingBuilder,
                resolved_version: Optional[str], chain: List[str]) -> bool:
    """Validate a declared semver spec against the resolved lock version."""
    try:
        # parse first; raises on unsupported syntax
        from .semver import parse_range
        parse_range(spec)
    except UnsupportedSpecError as exc:
        fb.unsupported_syntax(location, spec, str(exc))
        return False
    if resolved_version is None:
        return False
    try:
        parse_version(resolved_version)
    except UnsupportedSpecError:
        fb.add("invalid-version", "error", location,
               f"{node_name} lock version {resolved_version!r} is not valid semver",
               chain=chain, evidence={"version": resolved_version})
        return False
    if not satisfies(resolved_version, spec):
        fb.add("range-drift", "error", location,
               f"resolved {node_name}@{resolved_version} does not satisfy declared range {spec!r}",
               chain=chain,
               diff=json_diff(location, {"range": spec, "resolved": resolved_version,
                                         "satisfied": True},
                              {"range": spec, "resolved": resolved_version,
                               "satisfied": False}),
               evidence={"range": spec, "resolved": resolved_version})
        return False
    return True


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------

def _audit_workspaces(pkg: dict, pkg_path: str, nodes: Dict[str, LockNode],
                      archive: Archive, fb: _FindingBuilder) -> Dict[str, str]:
    """Return mapping workspace package name -> workspace directory."""
    ws_field = pkg.get("workspaces")
    patterns: List[str] = []
    if isinstance(ws_field, list):
        patterns = ws_field
    elif isinstance(ws_field, dict):
        patterns = ws_field.get("packages") or []
    elif ws_field is None:
        patterns = []
    else:
        fb.add("workspaces-shape", "error", f"{pkg_path}#workspaces",
               "'workspaces' must be an array or {packages:[...]} object")
        return {}

    for p in patterns:
        if not isinstance(p, str):
            fb.add("workspaces-shape", "error", f"{pkg_path}#workspaces",
                   f"workspace pattern must be a string, got {type(p).__name__}")
            continue
        try:
            from .workspace_glob import compile_pattern
            compile_pattern(p)
        except UnsupportedGlobError as exc:
            fb.unsupported_syntax(f"{pkg_path}#workspaces", p, str(exc))

    # directories that contain a package.json inside the archive
    dirs_with_pkg = sorted({
        path.rsplit("/package.json", 1)[0]
        for path in archive.files
        if path.endswith("/package.json") and path != "package.json"
    })
    try:
        ws_dirs = glob_expand([p for p in patterns if isinstance(p, str)], dirs_with_pkg)
    except UnsupportedGlobError:
        ws_dirs = []

    result: Dict[str, str] = {}
    for d in ws_dirs:
        data = archive.get(d + "/package.json")
        if data is None:
            continue
        try:
            sub = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            fb.add("workspace-package-json", "error", f"{d}/package.json",
                   f"workspace manifest unreadable: {exc}")
            continue
        nm = sub.get("name")
        ver = sub.get("version")
        if not nm:
            fb.add("workspace-no-name", "warning", f"{d}/package.json",
                   "workspace package has no name; it cannot be referenced as a dependency")
            continue
        result[nm] = d
        # lock must contain a symlink entry node_modules/<nm> -> d
        link_key = f"node_modules/{nm}"
        lock_entry = nodes.get(link_key)
        if lock_entry is None:
            fb.add("workspace-not-locked", "error", f"package-lock.json#{link_key}",
                   f"workspace {nm!r} ({d}) has no matching symlink entry in the lockfile")
            continue
        if not lock_entry.link:
            fb.add("workspace-not-link", "error", f"package-lock.json#{link_key}",
                   f"lock entry for workspace {nm!r} is not marked \"link\": true")
            continue
        # npm records link targets relative to the link's own directory, e.g.
        # node_modules/@mono/lib -> ../../packages/lib ; normalize both.
        import posixpath
        target = posixpath.normpath(
            posixpath.join(posixpath.dirname(link_key), (lock_entry.link_target or ""))
        ).rstrip("/")
        if target != d:
            fb.add("workspace-link-drift", "error", f"package-lock.json#{link_key}",
                   f"workspace link target {target!r} does not match directory {d!r}",
                   evidence={"lock_target": lock_entry.link_target,
                             "normalized": target, "workspace_dir": d})
        if lock_entry.version != ver:
            fb.add("workspace-version-drift", "warning", f"package-lock.json#{link_key}",
                   f"workspace {nm} lock version {lock_entry.version!r} != manifest {ver!r}",
                   evidence={"lock_version": lock_entry.version, "manifest_version": ver})
        # workspace members' own dependencies must be represented in the lock
        root_node = lock_entry  # link node; its deps live at the real target key in v3?
        # In v3 the link target carries dependency metadata at the link key itself;
        # npm stores deps under the link node AND under target if it's also a pkg root.
        for dep, spec in (sub.get("dependencies") or {}).items():
            resolved_key = _resolve_key(nodes, link_key, dep)
            if resolved_key is None:
                fb.add("workspace-dep-missing", "error",
                       f"{d}/package.json#dependencies/{dep}",
                       f"workspace {nm} dependency {dep}@{spec} has no resolved node in the lockfile")
                continue
            _check_spec(dep, str(spec), f"{d}/package.json#dependencies/{dep}",
                        fb, nodes[resolved_key].version,
                        [f"{nm}@{ver or '?'}"] )
    return result


# ---------------------------------------------------------------------------
# Tarball content verification
# ---------------------------------------------------------------------------

def _verify_artifacts(nodes: Dict[str, LockNode], archive: Archive,
                      chains: Dict[str, List[str]], fb: _FindingBuilder) -> Tuple[int, int, int]:
    """Re-hash every tarball shipped under vendor/ (or a package .tgz).

    Returns (registry_total, artifacts_supplied, artifacts_verified).
    """
    supplied = 0
    verified = 0
    total = 0
    for key, node in nodes.items():
        if key == ROOT_LOCK_KEY or node.link or _is_workspace_target_key(key):
            continue
        # only nodes whose resolved URL passed the https-registry gate are
        # expected to be registry tarballs with integrity metadata
        from urllib.parse import urlparse
        u = urlparse(node.resolved or "")
        if u.scheme != "https":
            continue
        total += 1
        loc = f"package-lock.json#packages/{key}"
        chain = [label(nodes, k) for k in chains.get(key, [])]
        try:
            integ = parse_integrity(node.integrity)
        except IntegrityError as exc:
            fb.add("missing-or-bad-integrity", "error", loc,
                   f"{node.name}@{node.version}: {exc}", chain=chain,
                   evidence={"integrity": node.integrity})
            continue
        candidate_paths = []
        if node.resolved:
            from urllib.parse import urlparse, unquote
            fname = unquote(urlparse(node.resolved).path.rsplit("/", 1)[-1])
            candidate_paths.append(f"vendor/{fname}")
            candidate_paths.append(f"node_modules/.cache/{fname}")
            candidate_paths.append(f"artifacts/{fname}")
        blob = None
        where = None
        for p in candidate_paths:
            blob = archive.get(p)
            if blob is not None:
                where = p
                break
        if blob is None:
            continue
        supplied += 1
        if verify(blob, integ):
            verified += 1
            fb.add("artifact-verified", "info", where,
                   f"{node.name}@{node.version} {integ.algorithm} digest recomputed and matched",
                   chain=chain)
        else:
            actual = compute_digest(blob, integ.algorithm)
            import base64
            fb.add("artifact-tampered", "error", where,
                   f"{node.name}@{node.version} tarball digest does not match lockfile integrity",
                   chain=chain,
                   evidence={
                       "declared": integ.sri,
                       "actual": f"{integ.algorithm}-{base64.b64encode(actual).decode()}",
                       "bytes": len(blob),
                   })
    return total, supplied, verified


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def audit(archive: Archive, *,
          target: Optional[Target] = None,
          include_dev: bool = True,
          allowed_registries: Optional[List[str]] = None,
          package_path: Optional[str] = None,
          lock_path: Optional[str] = None) -> dict:
    fb = _FindingBuilder()
    registries = allowed_registries or ["registry.npmjs.org"]
    try:
        pkg_path, pkg, lock_path_found, lock, audited = load_bundle(
            archive, package_path=package_path, lock_path=lock_path)
    except FileNotFoundError as exc:
        return _result(fb, load_error=f"required file missing: {exc}", audited=[])
    except ValueError as exc:
        return _result(fb, load_error=str(exc), audited=[])

    # lockfile version gate — hard rejection of anything but v3
    lfv = lock.get("lockfileVersion")
    if lfv != SUPPORTED_LOCKFILE_VERSION:
        fb.unsupported_syntax(
            f"{lock_path_found}#lockfileVersion",
            f"lockfileVersion={lfv!r}",
            f"only lockfileVersion {SUPPORTED_LOCKFILE_VERSION} is supported; "
            "v1/v2 layouts must be regenerated with a current npm",
        )
        return _result(fb, pkg=pkg, lock=lock, audited=audited)

    if lock.get("requires") is not None and not isinstance(lock.get("requires"), bool):
        fb.add("lock-structure", "error", f"{lock_path_found}#requires",
               "'requires' must be boolean when present")

    nodes = _build_lock_nodes(lock, fb)
    chains = shortest_chains(nodes)

    # ---- root identity --------------------------------------------------
    root = nodes.get(ROOT_LOCK_KEY)
    root_name = pkg.get("name")
    root_version = pkg.get("version")
    if root is None:
        fb.add("lock-structure", "error", f"{lock_path_found}#packages",
               "lockfile lacks the root packages[\"\"] entry")
    else:
        if root_name and root.name and root.name != root_name:
            fb.add("root-name-drift", "error", f"{lock_path_found}#packages/",
                   f"root name mismatch: package.json says {root_name!r}, lock says {root.name!r}",
                   evidence={"package_json": root_name, "lock": root.name})
        if root_version and root.version and root.version != root_version:
            fb.add("root-version-drift", "warning", f"{lock_path_found}#packages/",
                   f"root version drift: package.json {root_version!r}, lock {root.version!r}",
                   evidence={"package_json": root_version, "lock": root.version})

    # ---- workspaces -----------------------------------------------------
    ws_map = _audit_workspaces(pkg, pkg_path, nodes, archive, fb)
    # link nodes for declared workspaces are reachable from the project root
    for ws_name in ws_map:
        lk = f"node_modules/{ws_name}"
        if lk in nodes and lk not in chains:
            chains[lk] = [ROOT_LOCK_KEY]

    # ---- root dependency declaration consistency ------------------------
    declared = {
        "dependencies": dict(pkg.get("dependencies") or {}),
        "optionalDependencies": dict(pkg.get("optionalDependencies") or {}),
        "devDependencies": dict(pkg.get("devDependencies") or {}),
        "peerDependencies": dict(pkg.get("peerDependencies") or {}),
    }
    # shape checks
    for sec in ("dependencies", "optionalDependencies", "devDependencies", "peerDependencies"):
        val = pkg.get(sec)
        if val is not None and not isinstance(val, dict):
            fb.add("manifest-shape", "error", f"{pkg_path}#{sec}",
                   f"{sec} must be an object")

    # package.json optional deps take precedence over plain deps in npm;
    # flag a name appearing in both (npm deletes it from dependencies)
    both = set(declared["dependencies"]) & set(declared["optionalDependencies"])
    for nm in sorted(both):
        fb.add("dep-in-both-sections", "warning",
               f"{pkg_path}#optionalDependencies/{nm}",
               f"{nm} is listed in both dependencies and optionalDependencies; npm treats it as optional",
               evidence={"dependencies": declared["dependencies"][nm],
                         "optionalDependencies": declared["optionalDependencies"][nm]})

    def root_dep_check(dep: str, spec, section: str, optional: bool, dev: bool):
        loc = f"{pkg_path}#{section}/{dep}"
        if not isinstance(spec, str):
            fb.add("manifest-shape", "error", loc,
                   f"version spec must be a string, got {type(spec).__name__}")
            return
        # Syntax is validated against the declaration itself even when the
        # lock entry is missing, so unsupported syntax is never hidden.
        try:
            from .semver import parse_range
            parse_range(spec)
            syntax_ok = True
        except UnsupportedSpecError as exc:
            fb.unsupported_syntax(loc, spec, str(exc))
            syntax_ok = False
        if dep in ws_map:
            # workspace dependency: lock must be the link node
            link_key = f"node_modules/{dep}"
            ln = nodes.get(link_key)
            if ln is None or not ln.link:
                fb.add("workspace-dep-not-link", "error", loc,
                       f"{dep} is a workspace member but the lock has no link node at {link_key}")
            return
        key = _resolve_key(nodes, ROOT_LOCK_KEY, dep)
        if key is None:
            if optional:
                # An optional dependency may legitimately be omitted on the
                # target platform (recorded as info, never a hard failure).
                fb.add("optional-not-locked", "info", loc,
                       f"optional dependency {dep}@{spec} has no lock node "
                       "(expected when excluded on the target platform)",
                       evidence={"spec": spec})
            else:
                fb.add("declared-not-locked", "error", loc,
                       f"{dep}@{spec} is declared but absent from the lockfile",
                       evidence={"spec": spec})
            return
        node = nodes[key]
        chain = [label(nodes, k) for k in chains.get(key, [])]
        if node.link:
            fb.add("declared-resolves-to-link", "error", loc,
                   f"{dep}@{spec} resolves to a bare link node {node.link_target!r}, not a workspace member")
            return
        if syntax_ok:
            _check_spec(dep, spec, loc, fb, node.version, chain)

    for dep, spec in declared["dependencies"].items():
        root_dep_check(dep, spec, "dependencies", False, False)
    for dep, spec in declared["optionalDependencies"].items():
        root_dep_check(dep, spec, "optionalDependencies", True, False)
    if include_dev:
        for dep, spec in declared["devDependencies"].items():
            root_dep_check(dep, spec, "devDependencies", False, True)

    # lock root entries that are not declared anywhere => lock drift
    declared_all = (set(declared["dependencies"])
                    | set(declared["optionalDependencies"])
                    | (set(declared["devDependencies"]) if include_dev else set())
                    | set(ws_map))
    root_lock = nodes.get(ROOT_LOCK_KEY)
    if root_lock:
        all_root_deps = (set(root_lock.deps) | set(root_lock.optional_deps)
                         | (set(root_lock.dev_deps) if include_dev else set()))
        for dep in sorted(all_root_deps - declared_all):
            fb.add("locked-not-declared", "warning",
                   f"package-lock.json#packages/{dep}",
                   f"{dep} is in the lockfile but not declared in package.json",
                   evidence={"locked_spec":
                             root_lock.deps.get(dep)
                             or root_lock.optional_deps.get(dep)
                             or root_lock.dev_deps.get(dep)})

    # ---- per-node checks ------------------------------------------------
    parents, edge_optional = _parent_tree(nodes)
    reachable: Set[str] = set(chains.keys())
    versions_by_name: Dict[str, List[str]] = defaultdict(list)
    for key, node in nodes.items():
        if key == ROOT_LOCK_KEY:
            continue
        is_link = node.link
        is_ws_target = _is_workspace_target_key(key)
        loc = f"package-lock.json#packages/{key}"
        chain = [label(nodes, k) for k in chains.get(key, [])]
        if not is_link and not is_ws_target:
            versions_by_name[node.name].append(node.version or "?")
            # registry metadata; only true registry tarball nodes carry an
            # integrity requirement. git/file/etc. nodes are already reported
            # as unsupported syntax and must not cascade further errors.
            is_registry = _check_resolved(node, registries, fb, chain, loc)
            if is_registry:
                # version validity
                if node.version is None:
                    fb.add("invalid-version", "error", loc,
                           f"{node.name} lock entry has no version", chain=chain)
                else:
                    try:
                        parse_version(node.version)
                    except UnsupportedSpecError:
                        fb.add("invalid-version", "error", loc,
                               f"{node.name} version {node.version!r} is not valid semver", chain=chain)

        # unreachable node (not referenced from root tree)
        if key not in reachable and not is_ws_target:
            fb.add("unreachable-node", "warning", loc,
                   f"{node.name}@{node.version} exists in the lockfile but is not reachable "
                   "from any root dependency (stale/phantom entry)", chain=[])

        # nested dependency spec consistency (declared by each parent).
        # Root-parent edges were already checked in root_dep_check; skip them
        # here so a mismatch is reported exactly once.
        parent_set = parents.get(key, set())
        for pkey in sorted(parent_set):
            if pkey == ROOT_LOCK_KEY:
                continue
            pnode = nodes[pkey]
            spec = None
            section = None
            if node.name in pnode.deps:
                spec, section = pnode.deps[node.name], "dependencies"
            elif node.name in pnode.optional_deps:
                spec, section = pnode.optional_deps[node.name], "optionalDependencies"
            if spec is None:
                continue
            ploc = f"package-lock.json#packages/{pkey}/{section}/{node.name}"
            pchain = [label(nodes, k) for k in chains.get(pkey, [])] + [label(nodes, pkey)]
            _check_spec(node.name, spec, ploc, fb, node.version, pchain)

        if not is_link and not is_ws_target:
            # platform optionality
            if target is not None and (node.os or node.cpu or node.libc):
                reason = platform_evaluate(
                    {"os": node.os, "cpu": node.cpu, "libc": node.libc}, target)
                if reason:
                    only_via_optional = all(
                        edge_optional.get((p, key), True) for p in parents.get(key, set())
                    ) if parents.get(key) else node.optional
                    sev = "info" if (node.optional or only_via_optional) else "error"
                    fb.add("platform-excluded" if sev != "error" else "platform-required-excluded",
                           sev, loc,
                           f"{node.name}@{node.version} excluded on target "
                           f"{target.os}/{target.cpu}"
                           + (f"/{target.libc}" if target.libc else "")
                           + f": {reason}",
                           chain=chain, evidence={"os": node.os, "cpu": node.cpu,
                                                  "libc": node.libc})

            # install scripts are never run by this gate; make that explicit so
            # the report cannot be mistaken for having executed them
            if node.has_install_script:
                fb.add("install-script-not-run", "info", loc,
                       f"{node.name}@{node.version} declares a lifecycle script "
                       f"({sorted(k for k in ('preinstall','install','postinstall') if k in node.scripts)}); "
                       "the gate never executes install scripts",
                       chain=chain)
            if node.bin:
                fb.add("bin-not-linked", "info", loc,
                       f"{node.name}@{node.version} ships bin entries; they are not linked or executed",
                       chain=chain)

    # ---- duplicate versions --------------------------------------------
    duplicates = {}
    for nm, vers in sorted(versions_by_name.items()):
        unique = sorted(set(vers))
        if len(unique) > 1:
            duplicates[nm] = unique
            keys = [k for k in nodes if not nodes[k].link and nodes[k].name == nm]
            fb.add("duplicate-versions", "warning",
                   f"package-lock.json#packages ({nm})",
                   f"{nm} resolves to {len(unique)} distinct versions: {', '.join(unique)}",
                   chain=[], evidence={"versions": unique, "keys": sorted(keys)})

    # ---- cycles ---------------------------------------------------------
    cycles = find_cycles(nodes)
    cycle_label_sets = []
    for cyc_keys, cyc_labels in cycles:
        cycle_label_sets.append(cyc_labels)
        # shortest chain explaining how the cycle is reached from the root
        entry_key = min(cyc_keys, key=lambda k: (len(chains.get(k, [])), k))
        root_chain = [label(nodes, k) for k in chains.get(entry_key, [])]
        fb.add("dependency-cycle", "warning",
               "package-lock.json#packages",
               "dependency cycle detected: " + " -> ".join(cyc_labels + [cyc_labels[0]]),
               chain=root_chain + cyc_labels)

    # ---- cryptographic content verification ----------------------------
    registry_total, supplied, verified = _verify_artifacts(nodes, archive, chains, fb)

    # ---- reproducibility ------------------------------------------------
    pinned = True
    reasons: List[str] = []
    error_codes = {f["code"] for f in fb.items if f["severity"] == "error"}
    for code, why in (
        ("missing-or-bad-integrity", "at least one package lacks a valid integrity digest"),
        ("missing-resolved", "at least one package lacks a resolved URL"),
        ("non-https-resolved", "at least one resolved URL is not https"),
        ("untrusted-registry", "at least one resolved URL points outside the registry allowlist"),
        ("unsupported-syntax", "the manifest uses dependency syntax this gate refuses"),
        ("range-drift", "a locked version is outside its declared semver range"),
        ("declared-not-locked", "a declared dependency is absent from the lockfile"),
        ("artifact-tampered", "a supplied artifact failed digest verification"),
    ):
        if code in error_codes:
            pinned = False
            reasons.append(why)
    content_verified = supplied > 0 and supplied == verified and registry_total == supplied
    if registry_total and supplied == 0:
        reasons.append("no tarball artifacts were supplied, so byte-level verification was not performed; "
                       "the presence of a lockfile alone is not proof of reproducibility")
    elif supplied < registry_total:
        pinned = False
        content_verified = False
        reasons.append(f"only {supplied}/{registry_total} registry packages had artifacts to verify")
    if supplied != verified:
        pinned = False
        content_verified = False

    reproducible = pinned and content_verified
    if not supported_lockfile_consistency(fb):
        reasons.insert(0, "package.json and package-lock.json are not in consistent state")

    summary = {
        "root_name": root_name,
        "root_version": root_version,
        "lockfile_version": lfv,
        "total_packages": registry_total,
        "registry_packages": registry_total,
        "workspace_links": len([1 for n in nodes.values() if n.link]),
        "optional_packages": len([1 for n in nodes.values() if n.optional]),
        "duplicate_names": duplicates,
        "cycles": cycle_label_sets,
        "platform": ({"os": target.os, "cpu": target.cpu, "libc": target.libc}
                     if target else {"evaluated": False}),
        "dev_included": include_dev,
        "artifacts_supplied": supplied,
        "artifacts_verified": verified,
    }

    return {
        "ok": not any(f["severity"] == "error" for f in fb.items),
        "reproducibility": {
            "lockfile_present": True,
            "pinned": pinned,
            "content_verified": content_verified,
            "reproducible": reproducible,
            "reasons": reasons,
        },
        "summary": summary,
        "findings": sorted(fb.items, key=lambda f: ({"error": 0, "warning": 1, "info": 2}[f["severity"]],
                                                    f["code"], f["location"])),
        "unsupported_syntax": fb.unsupported,
        "audited_files": audited,
    }


def supported_lockfile_consistency(fb: _FindingBuilder) -> bool:
    hard = {"range-drift", "declared-not-locked", "root-name-drift",
            "workspace-not-locked", "workspace-not-link", "workspace-link-drift",
            "workspace-dep-missing", "artifact-tampered", "missing-or-bad-integrity",
            "missing-resolved", "non-https-resolved", "untrusted-registry",
            "unsupported-syntax", "platform-required-excluded"}
    return not any(f["severity"] == "error" and f["code"] in hard for f in fb.items)


def _result(fb: _FindingBuilder, *, load_error: Optional[str] = None,
            pkg: Optional[dict] = None, lock: Optional[dict] = None,
            audited: Optional[List[str]] = None) -> dict:
    if load_error:
        fb.add("bundle-unreadable", "error", "<input>", load_error)
    return {
        "ok": False,
        "reproducibility": {
            "lockfile_present": bool(lock),
            "pinned": False,
            "content_verified": False,
            "reproducible": False,
            "reasons": [load_error] if load_error else ["audit aborted before completion"],
        },
        "summary": {
            "root_name": (pkg or {}).get("name"),
            "root_version": (pkg or {}).get("version"),
            "lockfile_version": (lock or {}).get("lockfileVersion") if lock else None,
            "total_packages": 0,
            "registry_packages": 0,
            "workspace_links": 0,
            "optional_packages": 0,
            "duplicate_names": {},
            "cycles": [],
            "platform": {"evaluated": False},
            "dev_included": True,
            "artifacts_supplied": 0,
            "artifacts_verified": 0,
        },
        "findings": fb.items,
        "unsupported_syntax": fb.unsupported,
        "audited_files": audited or [],
    }
