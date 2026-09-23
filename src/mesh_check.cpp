#include "mesh_check.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <map>
#include <numeric>
#include <set>
#include <unordered_map>
#include <utility>

namespace meshcheck {

namespace {

struct DSU {
    std::vector<int64_t> parent;
    explicit DSU(int64_t n) : parent(static_cast<size_t>(n)) {
        std::iota(parent.begin(), parent.end(), 0);
    }
    int64_t find(int64_t x) {
        while (parent[static_cast<size_t>(x)] != x) {
            parent[static_cast<size_t>(x)] = parent[static_cast<size_t>(parent[static_cast<size_t>(x)])];
            x = parent[static_cast<size_t>(x)];
        }
        return x;
    }
    void unite(int64_t a, int64_t b) {
        a = find(a); b = find(b);
        if (a != b) parent[static_cast<size_t>(b)] = a;
    }
};

double vdist(const Vec3& a, const Vec3& b) {
    double s = 0.0;
    for (int k = 0; k < 3; ++k) {
        double d = a[k] - b[k];
        s += d * d;
    }
    return std::sqrt(s);
}

double triArea(const Vec3& a, const Vec3& b, const Vec3& c) {
    double u[3], v[3];
    for (int k = 0; k < 3; ++k) { u[k] = b[k] - a[k]; v[k] = c[k] - a[k]; }
    double cx = u[1] * v[2] - u[2] * v[1];
    double cy = u[2] * v[0] - u[0] * v[2];
    double cz = u[0] * v[1] - u[1] * v[0];
    return 0.5 * std::sqrt(cx * cx + cy * cy + cz * cz);
}

int64_t asInt(const json::Value& v) {
    if (v.type == json::Value::Num && std::isfinite(v.n) && v.n >= -9.0e18 && v.n <= 9.0e18
        && v.n == std::floor(v.n)) {
        return static_cast<int64_t>(v.n);
    }
    return std::numeric_limits<int64_t>::min();
}

} // namespace

Report runChecks(const json::Value& req, const Options& opts) {
    Report r;

    const json::Value* pts = req.find("points");
    const json::Value* fcs = req.find("faces");
    if (!pts || pts->type != json::Value::Arr) {
        r.requestErrors.push_back("request: missing or non-array \"points\" (array of [x,y,z])");
        return r;
    }
    if (!fcs || fcs->type != json::Value::Arr) {
        r.requestErrors.push_back("request: missing or non-array \"faces\" (array of index triples)");
        return r;
    }

    // ---- Parse points -----------------------------------------------------
    r.vertexCountInput = static_cast<int64_t>(pts->a.size());
    std::vector<Vec3> coords;
    coords.reserve(pts->a.size());
    std::vector<bool> pointValid;
    pointValid.reserve(pts->a.size());
    Vec3 mn{{std::numeric_limits<double>::infinity(),
             std::numeric_limits<double>::infinity(),
             std::numeric_limits<double>::infinity()}};
    Vec3 mx{{-std::numeric_limits<double>::infinity(),
             -std::numeric_limits<double>::infinity(),
             -std::numeric_limits<double>::infinity()}};

    for (size_t i = 0; i < pts->a.size(); ++i) {
        const json::Value& p = pts->a[i];
        Vec3 c{{0.0, 0.0, 0.0}};
        bool ok = p.type == json::Value::Arr && p.a.size() == 3;
        if (ok) {
            for (int k = 0; k < 3; ++k) {
                if (p.a[k].type != json::Value::Num || !std::isfinite(p.a[k].n)) { ok = false; break; }
                c[k] = p.a[k].n;
            }
        }
        if (!ok) {
            r.requestErrors.push_back("points[" + std::to_string(i) +
                                      "]: expected three finite numbers [x,y,z]");
            pointValid.push_back(false);
            coords.push_back(c);
            continue;
        }
        pointValid.push_back(true);
        coords.push_back(c);
        for (int k = 0; k < 3; ++k) {
            mn[k] = std::min(mn[k], c[k]);
            mx[k] = std::max(mx[k], c[k]);
        }
    }

    double diag = 0.0;
    if (std::isfinite(mn[0])) {
        double s = 0.0;
        for (int k = 0; k < 3; ++k) s += (mx[k] - mn[k]) * (mx[k] - mn[k]);
        diag = std::sqrt(s);
    }
    // Degeneracy tolerance: absolute floor plus a relative (bbox) term.
    const double tol = opts.epsAbs + opts.epsRel * diag;
    // Vertex-merge threshold. eps_rel <= 0 disables merging entirely.
    const bool mergeEnabled = opts.epsRel > 0.0;
    const double mergeTol = std::max(opts.epsAbs, opts.epsRel * diag);

    // ---- Merge coincident vertices ---------------------------------------
    // rep[i] = canonical vertex index for input vertex i
    std::vector<int64_t> rep(coords.size());
    std::iota(rep.begin(), rep.end(), 0);
    if (mergeEnabled && pointValid.size() > 1) {
        // O(n^2); datasets here are small. Grid hashing would replace this at scale.
        for (size_t i = 0; i < coords.size(); ++i) {
            if (!pointValid[i] || rep[i] != static_cast<int64_t>(i)) continue;
            for (size_t j = i + 1; j < coords.size(); ++j) {
                if (!pointValid[j] || rep[j] != static_cast<int64_t>(j)) continue;
                if (vdist(coords[i], coords[j]) <= mergeTol) {
                    rep[j] = static_cast<int64_t>(i);
                    r.duplicateVertices.emplace_back(static_cast<int64_t>(i),
                                                     static_cast<int64_t>(j));
                }
            }
        }
    }

    // ---- Parse faces ------------------------------------------------------
    r.faceCountInput = static_cast<int64_t>(fcs->a.size());
    std::vector<Face> faces;
    faces.reserve(fcs->a.size());

    for (size_t fi = 0; fi < fcs->a.size(); ++fi) {
        const json::Value& f = fcs->a[fi];
        const json::Value* idxVal = nullptr;
        int64_t id = static_cast<int64_t>(fi);

        if (f.type == json::Value::Arr) {
            idxVal = &f;
        } else if (f.type == json::Value::Obj) {
            idxVal = f.find("verts");
            if (!idxVal) idxVal = f.find("vertices");
            if (!idxVal) idxVal = f.find("indices");
            if (const json::Value* idv = f.find("id")) {
                int64_t v = asInt(*idv);
                if (v == std::numeric_limits<int64_t>::min()) {
                    r.requestErrors.push_back("faces[" + std::to_string(fi) +
                                              "]: \"id\" must be an integer");
                } else {
                    id = v;
                }
            }
        }

        if (!idxVal || idxVal->type != json::Value::Arr || idxVal->a.size() != 3) {
            r.requestErrors.push_back("faces[" + std::to_string(fi) +
                                      "]: expected three vertex indices, e.g. [0,1,2] or "
                                      "{\"id\":1,\"verts\":[0,1,2]}");
            continue;
        }

        Face face;
        face.id = id;
        face.inputIndex = fi;
        bool ok = true;
        for (int k = 0; k < 3; ++k) {
            int64_t v = asInt(idxVal->a[k]);
            if (v == std::numeric_limits<int64_t>::min() || v < 0 ||
                static_cast<size_t>(v) >= coords.size()) {
                r.requestErrors.push_back("faces[" + std::to_string(fi) + "].verts[" +
                                          std::to_string(k) + "]: index " +
                                          (idxVal->a[k].type == json::Value::Num
                                               ? std::to_string(idxVal->a[k].n)
                                               : "<non-integer>") +
                                          " out of range [0," +
                                          std::to_string(coords.size() - 1) + "]");
                ok = false;
                break;
            }
            if (!pointValid[static_cast<size_t>(v)]) {
                r.requestErrors.push_back("faces[" + std::to_string(fi) +
                                          "]: references invalid point " + std::to_string(v));
                ok = false;
                break;
            }
            face.origVerts[k] = v;
            face.verts[k] = rep[static_cast<size_t>(v)];
        }
        if (!ok) continue;

        // Repeated corner (after merge) is a geometric degeneracy.
        face.area = triArea(coords[static_cast<size_t>(face.verts[0])],
                            coords[static_cast<size_t>(face.verts[1])],
                            coords[static_cast<size_t>(face.verts[2])]);
        double longest = 0.0;
        for (int k = 0; k < 3; ++k) {
            longest = std::max(longest,
                               vdist(coords[static_cast<size_t>(face.verts[k])],
                                     coords[static_cast<size_t>(face.verts[(k + 1) % 3])]));
        }
        bool repeatedCorner = face.verts[0] == face.verts[1] ||
                              face.verts[1] == face.verts[2] ||
                              face.verts[0] == face.verts[2];
        // Area threshold: triangle whose height is within tol of its base,
        // plus exact repeated-corner collapse.
        face.degenerate = repeatedCorner || face.area <= 0.5 * tol * longest;
        if (face.degenerate) r.degenerateFaces.push_back(face.id);
        faces.push_back(std::move(face));
    }
    r.faceCountAccepted = static_cast<int64_t>(faces.size());

    // ---- Duplicate faces (same set of 3 canonical vertices) --------------
    // Degenerate faces are geometric issues and are excluded from the
    // topological duplicate-face analysis.
    std::map<std::array<int64_t, 3>, std::vector<size_t>> faceByKey;
    for (size_t i = 0; i < faces.size(); ++i) {
        if (faces[i].degenerate) continue;
        std::array<int64_t, 3> key = faces[i].verts;
        std::sort(key.begin(), key.end());
        faceByKey[key].push_back(i);
    }
    std::vector<char> isDuplicateCopy(faces.size(), false);
    for (auto& [key, group] : faceByKey) {
        if (group.size() < 2) continue;
        DuplicateFaceGroup g;
        for (size_t k = 0; k < group.size(); ++k) {
            g.faceIds.push_back(faces[group[k]].id);
            if (k > 0) {
                isDuplicateCopy[group[k]] = true;
                r.duplicateFaceIds.push_back(faces[group[k]].id);
            }
        }
        r.duplicateFaceGroups.push_back(std::move(g));
    }
    std::sort(r.duplicateFaceIds.begin(), r.duplicateFaceIds.end());
    std::sort(r.degenerateFaces.begin(), r.degenerateFaces.end());

    // ---- Directed half-edges / undirected edges --------------------------
    // Analysis set: accepted, non-degenerate, first copy of each duplicate.
    std::vector<size_t> active;
    for (size_t i = 0; i < faces.size(); ++i) {
        if (!faces[i].degenerate && !isDuplicateCopy[i]) active.push_back(i);
    }
    r.faceCountTriangulated = static_cast<int64_t>(active.size());

    std::map<std::pair<int64_t, int64_t>, EdgeInfo> edges;
    std::map<std::pair<int64_t, int64_t>, size_t> edgeFirstFace; // edge -> first local face
    // directed half-edge -> list of local face indices (for winding tests)
    std::map<std::pair<int64_t, int64_t>, std::vector<size_t>> halfedges;

    DSU dsu(static_cast<int64_t>(faces.size()));
    std::vector<int64_t> rootToComp(faces.size(), -1);

    for (size_t fi : active) {
        const Face& f = faces[fi];
        for (int k = 0; k < 3; ++k) {
            int64_t u = f.verts[k], v = f.verts[(k + 1) % 3];
            halfedges[{u, v}].push_back(fi);
            int64_t a = std::min(u, v), b = std::max(u, v);
            std::pair<int64_t, int64_t> ekey{a, b};
            auto it = edges.find(ekey);
            if (it == edges.end()) {
                EdgeInfo e; e.a = a; e.b = b;
                it = edges.emplace(ekey, std::move(e)).first;
                edgeFirstFace[ekey] = fi;
            } else {
                // Faces sharing any undirected edge belong to one component.
                dsu.unite(static_cast<int64_t>(fi),
                          static_cast<int64_t>(edgeFirstFace[ekey]));
            }
            it->second.faceIds.push_back(f.id);
        }
    }

    // ---- Connected components (over active faces) ------------------------
    std::vector<int64_t> compId(faces.size(), -1);
    int64_t nextComp = 0;
    for (size_t fi : active) {
        int64_t root = dsu.find(static_cast<int64_t>(fi));
        int64_t& cid = rootToComp[static_cast<size_t>(root)];
        if (cid < 0) cid = nextComp++;
        compId[fi] = cid;
    }
    std::vector<Component> comps(static_cast<size_t>(nextComp));
    for (auto& c : comps) c.closed = true;
    for (size_t fi : active)
        comps[static_cast<size_t>(compId[fi])].faceCount++;
    std::vector<std::set<int64_t>> compVerts(comps.size());
    for (size_t fi : active)
        for (int64_t v : faces[fi].verts)
            compVerts[static_cast<size_t>(compId[fi])].insert(v);
    for (size_t i = 0; i < comps.size(); ++i)
        comps[i].vertexCount = static_cast<int64_t>(compVerts[i].size());

    // ---- Classify edges, attach component, tally component edge counts ----
    // Done in one pass; edges are moved into the report vectors.
    for (auto& [key, e] : edges) {
        std::sort(e.faceIds.begin(), e.faceIds.end());
        int64_t cid = compId[edgeFirstFace.at(key)];
        e.comp = cid;
        Component& c = comps[static_cast<size_t>(cid)];
        size_t inc = e.faceIds.size();
        if (inc == 1) {
            c.boundaryEdges++;
            c.closed = false;
            r.boundaryEdges.push_back(std::move(e));
        } else if (inc >= 3) {
            c.nonManifoldEdges++;
            c.closed = false;
            r.nonManifoldEdges.push_back(std::move(e));
        } else {
            // Exactly two incident faces: consistent manifold pairing requires
            // opposite winding directions; same direction = orientation conflict.
            auto fwd = halfedges.find({e.a, e.b});
            auto rev = halfedges.find({e.b, e.a});
            size_t nFwd = fwd == halfedges.end() ? 0 : fwd->second.size();
            size_t nRev = rev == halfedges.end() ? 0 : rev->second.size();
            if (nFwd == 1 && nRev == 1) {
                // well-oriented shared edge: nothing to report
            } else {
                // Two incidences but same winding: orientation conflict.
                // "closed" stays a purely incidence-based (watertight) notion;
                // orientability is reported separately in the summary.
                r.orientationConflicts.push_back(std::move(e));
            }
        }
    }
    for (int64_t i = 0; i < static_cast<int64_t>(comps.size()); ++i) {
        comps[static_cast<size_t>(i)].id = i;
        r.components.push_back(comps[static_cast<size_t>(i)]);
    }

    // ---- Isolated vertices (canonical, used by no analyzed face) ---------
    // Vertices referenced only by degenerate faces or duplicate copies are
    // reported isolated: they carry no surface topology.
    std::vector<char> referenced(coords.size(), false);
    for (size_t fi : active)
        for (int64_t v : faces[fi].verts) referenced[static_cast<size_t>(v)] = true;
    for (size_t i = 0; i < coords.size(); ++i)
        if (pointValid[i] && rep[i] == static_cast<int64_t>(i) && !referenced[i])
            r.isolatedVertices.push_back(static_cast<int64_t>(i));

    // ---- Summary ----------------------------------------------------------
    r.manifold = r.nonManifoldEdges.empty() && r.orientationConflicts.empty() &&
                 r.duplicateFaceIds.empty();
    r.orientable = r.nonManifoldEdges.empty() && r.orientationConflicts.empty();
    r.closed = !r.components.empty();
    for (const Component& c : r.components) r.closed = r.closed && c.closed;

    return r;
}

json::Value buildResponse(const Report& r, const Options& opts) {
    json::Value root = json::Value::makeObj();

    bool hasTopoError = !r.nonManifoldEdges.empty() || !r.orientationConflicts.empty() ||
                        !r.duplicateFaceIds.empty();
    root.set("ok", json::Value::makeBool(r.requestErrors.empty() && !hasTopoError));

    json::Value cs = json::Value::makeObj();
    cs.set("name", json::Value::makeStr("right-handed Cartesian (local/metres, no datum transform)"));
    cs.set("axes", json::Value::makeStr("XYZ, right-handed; winding follows right-hand rule"));
    cs.set("eps_abs", json::Value::makeNum(opts.epsAbs));
    cs.set("eps_rel", json::Value::makeNum(opts.epsRel));
    cs.set("arithmetic", json::Value::makeStr("IEEE-754 double precision"));
    root.set("coordinate_system", cs);

    json::Value summary = json::Value::makeObj();
    summary.set("vertices_input", json::Value::makeNum(static_cast<double>(r.vertexCountInput)));
    summary.set("faces_input", json::Value::makeNum(static_cast<double>(r.faceCountInput)));
    summary.set("faces_accepted", json::Value::makeNum(static_cast<double>(r.faceCountAccepted)));
    summary.set("faces_analyzed", json::Value::makeNum(static_cast<double>(r.faceCountTriangulated)));
    summary.set("component_count", json::Value::makeNum(static_cast<double>(r.components.size())));
    summary.set("non_manifold_edge_count", json::Value::makeNum(static_cast<double>(r.nonManifoldEdges.size())));
    summary.set("orientation_conflict_count", json::Value::makeNum(static_cast<double>(r.orientationConflicts.size())));
    summary.set("duplicate_face_count", json::Value::makeNum(static_cast<double>(r.duplicateFaceIds.size())));
    summary.set("degenerate_face_count", json::Value::makeNum(static_cast<double>(r.degenerateFaces.size())));
    summary.set("duplicate_vertex_count", json::Value::makeNum(static_cast<double>(r.duplicateVertices.size())));
    summary.set("boundary_edge_count", json::Value::makeNum(static_cast<double>(r.boundaryEdges.size())));
    summary.set("isolated_vertex_count", json::Value::makeNum(static_cast<double>(r.isolatedVertices.size())));
    summary.set("manifold", json::Value::makeBool(r.manifold));
    summary.set("orientable", json::Value::makeBool(r.orientable));
    summary.set("closed", json::Value::makeBool(r.closed));
    root.set("summary", summary);

    json::Value errors = json::Value::makeObj();
    json::Value reqErrs = json::Value::makeArr();
    for (const auto& m : r.requestErrors) reqErrs.push(json::Value::makeStr(m));
    errors.set("request_errors", reqErrs);
    errors.set("duplicate_faces", [&] {
        json::Value a = json::Value::makeArr();
        for (int64_t id : r.duplicateFaceIds) a.push(json::Value::makeNum(static_cast<double>(id)));
        return a;
    }());
    errors.set("isolated_vertices", [&] {
        json::Value a = json::Value::makeArr();
        for (int64_t id : r.isolatedVertices) a.push(json::Value::makeNum(static_cast<double>(id)));
        return a;
    }());

    auto edgeList = [&](const std::vector<EdgeInfo>& es) {
        json::Value arr = json::Value::makeArr();
        for (const EdgeInfo& e : es) {
            json::Value o = json::Value::makeObj();
            json::Value edge = json::Value::makeArr();
            edge.push(json::Value::makeNum(static_cast<double>(e.a)));
            edge.push(json::Value::makeNum(static_cast<double>(e.b)));
            o.set("edge", edge);
            json::Value fids = json::Value::makeArr();
            for (int64_t f : e.faceIds) fids.push(json::Value::makeNum(static_cast<double>(f)));
            o.set("face_ids", fids);
            o.set("component", json::Value::makeNum(static_cast<double>(e.comp)));
            arr.push(o);
        }
        return arr;
    };
    errors.set("non_manifold_edges", edgeList(r.nonManifoldEdges));
    errors.set("orientation_conflicts", edgeList(r.orientationConflicts));
    root.set("errors", errors);

    // Boundary edges are informational (describe holes), not errors on their own.
    root.set("boundary_edges", edgeList(r.boundaryEdges));

    json::Value degen = json::Value::makeObj();
    json::Value df = json::Value::makeArr();
    for (int64_t id : r.degenerateFaces) df.push(json::Value::makeNum(static_cast<double>(id)));
    degen.set("degenerate_faces", df);
    json::Value dv = json::Value::makeArr();
    for (auto [kept, dropped] : r.duplicateVertices) {
        json::Value pair = json::Value::makeArr();
        pair.push(json::Value::makeNum(static_cast<double>(kept)));
        pair.push(json::Value::makeNum(static_cast<double>(dropped)));
        dv.push(pair);
    }
    degen.set("duplicate_vertices", dv);
    root.set("geometric_degeneracies", degen);

    json::Value groups = json::Value::makeArr();
    for (const DuplicateFaceGroup& g : r.duplicateFaceGroups) {
        json::Value a = json::Value::makeArr();
        for (int64_t id : g.faceIds) a.push(json::Value::makeNum(static_cast<double>(id)));
        groups.push(a);
    }
    root.set("duplicate_face_groups", groups);

    json::Value comps = json::Value::makeArr();
    for (const Component& c : r.components) {
        json::Value o = json::Value::makeObj();
        o.set("component", json::Value::makeNum(static_cast<double>(c.id)));
        o.set("face_count", json::Value::makeNum(static_cast<double>(c.faceCount)));
        o.set("vertex_count", json::Value::makeNum(static_cast<double>(c.vertexCount)));
        o.set("boundary_edges", json::Value::makeNum(static_cast<double>(c.boundaryEdges)));
        o.set("non_manifold_edges", json::Value::makeNum(static_cast<double>(c.nonManifoldEdges)));
        o.set("closed", json::Value::makeBool(c.closed));
        comps.push(o);
    }
    root.set("components", comps);

    return root;
}

} // namespace meshcheck
