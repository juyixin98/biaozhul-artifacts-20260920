#include "topology.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <map>
#include <numeric>
#include <unordered_map>
#include <vector>

namespace gridtopo {

using json::Value;

namespace {

// ---------- 并查集（重合点合并 / 连通分量）----------
struct DSU {
    std::vector<int> parent;
    std::vector<int> rankv;
    explicit DSU(int n) : parent(n), rankv(n, 0) {
        std::iota(parent.begin(), parent.end(), 0);
    }
    int find(int x) {
        while (parent[x] != x) {
            parent[x] = parent[parent[x]];
            x = parent[x];
        }
        return x;
    }
    bool unite(int a, int b) {
        a = find(a);
        b = find(b);
        if (a == b) return false;
        if (rankv[a] < rankv[b]) std::swap(a, b);
        parent[b] = a;
        if (rankv[a] == rankv[b]) ++rankv[a];
        return true;
    }
};

// 无向边编码：端点索引升序后压入 64 位整数。
uint64_t edgeKey(int a, int b) {
    if (a > b) std::swap(a, b);
    return (static_cast<uint64_t>(static_cast<uint32_t>(a)) << 32) |
           static_cast<uint32_t>(b);
}

// 三角形 (a,b,c) 相对顶点索引升序排列的置换奇偶：+1 同向，-1 反向。
int windingSign(const std::array<int, 3>& tri) {
    // 三个互异整数，按 (值, 位置) 排序后得到置换位置 p。
    std::pair<int, int> p[3] = {{tri[0], 0}, {tri[1], 1}, {tri[2], 2}};
    std::sort(p, p + 3);
    int order[3];
    for (int i = 0; i < 3; ++i) order[p[i].second] = i;
    int inversions = 0;
    for (int i = 0; i < 3; ++i)
        for (int j = i + 1; j < 3; ++j)
            if (order[i] > order[j]) ++inversions;
    return (inversions % 2 == 0) ? 1 : -1;
}

Value issueToJson(const Issue& iss) {
    Value v = Value::object();
    v.set("category", Value(iss.category));
    v.set("type", Value(iss.type));
    v.set("message", Value(iss.message));

    Value fids = Value::array();
    for (const auto& id : iss.faceIds) fids.push(Value(id));
    v.set("face_ids", fids);

    Value vids = Value::array();
    for (const auto& id : iss.vertexIds) vids.push(Value(id));
    v.set("vertex_ids", vids);

    if (iss.hasMetric) {
        Value m = Value::object();
        m.set("name", Value(iss.metricName));
        m.set("value", Value(iss.metricValue));
        v.set("metric", m);
    } else {
        v.set("metric", Value());
    }
    if (!iss.detail.empty()) v.set("detail", Value(iss.detail));
    return v;
}

} // namespace

ValidationReport validate(const Mesh& mesh) {
    ValidationReport rep;
    const int nv = static_cast<int>(mesh.vertices.size());
    const int nf = static_cast<int>(mesh.faces.size());
    rep.vertexCount = nv;
    rep.faceCount = nf;

    const double eps = mesh.config.eps;
    const double areaTol = mesh.config.areaEps;

    const std::string GEO = "geometric_degeneracy";
    const std::string TOPO = "topological_error";

    // ================================================================
    // 第一部分：几何退化检查（不参与后续拓扑分析）
    // ================================================================
    std::vector<bool> vertexFinite(nv, true);
    for (int i = 0; i < nv; ++i) {
        bool finite = std::isfinite(mesh.vertices[i].xyz[0]) &&
                      std::isfinite(mesh.vertices[i].xyz[1]) &&
                      std::isfinite(mesh.vertices[i].xyz[2]);
        vertexFinite[i] = finite;
        if (!finite) {
            Issue iss;
            iss.category = GEO;
            iss.type = "nonfinite_coordinate";
            iss.vertexIds.push_back(mesh.vertices[i].id);
            iss.message = "vertex '" + mesh.vertices[i].id + "' has a non-finite coordinate (NaN or infinity)";
            rep.geometricIssues.push_back(std::move(iss));
        }
    }

    // faceValid=false 的面只报告几何问题，从拓扑结构中剔除。
    std::vector<bool> faceValid(nf, true);
    for (int fi = 0; fi < nf; ++fi) {
        const Face& f = mesh.faces[fi];

        // (a) 重复顶点索引
        bool repeated = (f.v[0] == f.v[1] || f.v[1] == f.v[2] || f.v[0] == f.v[2]);
        if (repeated) {
            faceValid[fi] = false;
            Issue iss;
            iss.category = GEO;
            iss.type = "repeated_vertex_index";
            iss.faceIds.push_back(f.id);
            for (int vi : f.v) iss.vertexIds.push_back(mesh.vertices[vi].id);
            iss.message = "face '" + f.id + "' references the same vertex more than once";
            rep.geometricIssues.push_back(std::move(iss));
            continue;
        }

        // (b) 使用含非有限坐标的顶点
        bool usesNonFinite = !vertexFinite[f.v[0]] || !vertexFinite[f.v[1]] || !vertexFinite[f.v[2]];
        if (usesNonFinite) {
            faceValid[fi] = false;
            Issue iss;
            iss.category = GEO;
            iss.type = "face_uses_nonfinite_vertex";
            iss.faceIds.push_back(f.id);
            for (int vi : f.v) {
                if (!vertexFinite[vi]) iss.vertexIds.push_back(mesh.vertices[vi].id);
            }
            iss.message = "face '" + f.id + "' uses a vertex with non-finite coordinates; excluded from topology";
            rep.geometricIssues.push_back(std::move(iss));
            continue;
        }

        // (c) 零面积三角形：|AB × AC| / 2 <= areaTol
        const auto& A = mesh.vertices[f.v[0]].xyz;
        const auto& B = mesh.vertices[f.v[1]].xyz;
        const auto& C = mesh.vertices[f.v[2]].xyz;
        double ab[3] = {B[0] - A[0], B[1] - A[1], B[2] - A[2]};
        double ac[3] = {C[0] - A[0], C[1] - A[1], C[2] - A[2]};
        double cr[3] = {
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0]};
        double crossNorm = std::sqrt(cr[0] * cr[0] + cr[1] * cr[1] + cr[2] * cr[2]);
        double area = 0.5 * crossNorm;
        if (area <= areaTol) {
            faceValid[fi] = false;
            Issue iss;
            iss.category = GEO;
            iss.type = "zero_area_triangle";
            iss.faceIds.push_back(f.id);
            for (int vi : f.v) iss.vertexIds.push_back(mesh.vertices[vi].id);
            iss.hasMetric = true;
            iss.metricName = "area";
            iss.metricValue = area;
            iss.message = "face '" + f.id + "' is a zero-area (degenerate) triangle; excluded from topology";
            rep.geometricIssues.push_back(std::move(iss));
        }
    }

    // (d) 重合点（欧氏距离 <= eps）。按并查集聚类，每簇报告一次。
    DSU coincidentDSU(nv);
    std::vector<double> triggerMinDist(nv, -1.0);
    for (int i = 0; i < nv; ++i) {
        if (!vertexFinite[i]) continue;
        for (int j = i + 1; j < nv; ++j) {
            if (!vertexFinite[j]) continue;
            const auto& P = mesh.vertices[i].xyz;
            const auto& Q = mesh.vertices[j].xyz;
            double dx = P[0] - Q[0], dy = P[1] - Q[1], dz = P[2] - Q[2];
            double dist = std::sqrt(dx * dx + dy * dy + dz * dz);
            if (dist <= eps) coincidentDSU.unite(i, j);
        }
    }
    std::map<int, std::vector<int>> clusterMap;
    for (int i = 0; i < nv; ++i) {
        if (vertexFinite[i]) clusterMap[coincidentDSU.find(i)].push_back(i);
    }
    for (auto& kv : clusterMap) {
        const auto& members = kv.second;
        if (members.size() < 2) continue;
        // 簇内最大点距（展示传递合并的影响范围）
        double maxDist = 0.0;
        for (size_t a = 0; a < members.size(); ++a)
            for (size_t b = a + 1; b < members.size(); ++b) {
                const auto& P = mesh.vertices[members[a]].xyz;
                const auto& Q = mesh.vertices[members[b]].xyz;
                double dx = P[0] - Q[0], dy = P[1] - Q[1], dz = P[2] - Q[2];
                maxDist = std::max(maxDist, std::sqrt(dx * dx + dy * dy + dz * dz));
            }
        Issue iss;
        iss.category = GEO;
        iss.type = "coincident_vertices";
        for (int vi : members) iss.vertexIds.push_back(mesh.vertices[vi].id);
        iss.hasMetric = true;
        iss.metricName = "cluster_max_distance";
        iss.metricValue = maxDist;
        iss.message = std::to_string(members.size()) +
                      " vertices are coincident within eps (merged transitively; topology keeps original indices)";
        rep.geometricIssues.push_back(std::move(iss));
    }

    // ================================================================
    // 第二部分：拓扑检查（只含几何有效面）
    // ================================================================
    std::vector<int> validFaces;
    validFaces.reserve(nf);
    for (int fi = 0; fi < nf; ++fi) {
        if (faceValid[fi]) validFaces.push_back(fi);
    }

    // ---- 重复面：顶点集合相同 ----
    std::map<std::array<int, 3>, std::vector<int>> groups;
    for (int fi : validFaces) {
        std::array<int, 3> key = mesh.faces[fi].v;
        std::sort(key.begin(), key.end());
        groups[key].push_back(fi);
    }
    for (auto& kv : groups) {
        const auto& faceList = kv.second;
        if (faceList.size() < 2) continue;

        int positive = 0, negative = 0;
        for (int fi : faceList) {
            if (windingSign(mesh.faces[fi].v) > 0) ++positive;
            else ++negative;
        }
        std::string windingDetail;
        if (positive == 0 || negative == 0) windingDetail = "same_winding";
        else if (faceList.size() == 2) windingDetail = "opposite_winding";
        else windingDetail = "mixed_winding";

        rep.duplicateFaceExtraCount += static_cast<int>(faceList.size()) - 1;

        Issue iss;
        iss.category = TOPO;
        iss.type = "duplicate_face";
        for (int fi : faceList) iss.faceIds.push_back(mesh.faces[fi].id);
        for (int vi : kv.first) iss.vertexIds.push_back(mesh.vertices[vi].id);
        iss.hasMetric = true;
        iss.metricName = "copies";
        iss.metricValue = static_cast<double>(faceList.size());
        iss.detail = windingDetail;
        iss.message = std::to_string(faceList.size()) +
                      " faces share the exact same vertex set (" + windingDetail + ")";
        rep.topologicalIssues.push_back(std::move(iss));
    }

    // ---- 半边统计：每条无向边上的有向入射 ----
    struct EdgeInfo {
        int a = -1, b = -1;
        std::vector<int> forwardFaces; // 面沿 a->b 遍历
        std::vector<int> backwardFaces; // 面沿 b->a 遍历
    };
    std::map<uint64_t, EdgeInfo> edgeMap;

    for (int fi : validFaces) {
        const auto& v = mesh.faces[fi].v;
        for (int e = 0; e < 3; ++e) {
            int u = v[e], w = v[(e + 1) % 3];
            uint64_t key = edgeKey(u, w);
            auto it = edgeMap.find(key);
            if (it == edgeMap.end()) {
                EdgeInfo info;
                info.a = std::min(u, w);
                info.b = std::max(u, w);
                it = edgeMap.emplace(key, info).first;
            }
            if (u < w) it->second.forwardFaces.push_back(fi);
            else it->second.backwardFaces.push_back(fi);
        }
    }

    std::vector<uint64_t> boundaryEdgeKeys;
    int totalOrientationConflicts = 0;
    enum class EdgeKind { Boundary, Manifold, NonManifold };
    std::map<uint64_t, EdgeKind> edgeKind;

    for (auto& kv : edgeMap) {
        EdgeInfo& ei = kv.second;
        int incidences = static_cast<int>(ei.forwardFaces.size() + ei.backwardFaces.size());

        if (incidences == 1) {
            boundaryEdgeKeys.push_back(kv.first);
            edgeKind[kv.first] = EdgeKind::Boundary;
            continue;
        }

        bool sameDirectionPair = ei.forwardFaces.size() >= 2 || ei.backwardFaces.size() >= 2;

        if (incidences == 2 && !sameDirectionPair) {
            // 一正一反：流形且取向一致，无问题
            edgeKind[kv.first] = EdgeKind::Manifold;
            continue;
        }

        if (incidences >= 3 || sameDirectionPair) {
            // 入射面数 != 2（一正一反）即非流形边：包括 3+ 面共享，
            // 以及 2 个面同方向遍历同一条边（退化粘合，无法成为流形曲面）。
            edgeKind[kv.first] = EdgeKind::NonManifold;
            ++rep.nonManifoldEdgeCount;
            Issue iss;
            iss.category = TOPO;
            iss.type = "nonmanifold_edge";
            std::vector<int> all = ei.forwardFaces;
            all.insert(all.end(), ei.backwardFaces.begin(), ei.backwardFaces.end());
            std::sort(all.begin(), all.end());
            for (int fi : all) iss.faceIds.push_back(mesh.faces[fi].id);
            iss.vertexIds = {mesh.vertices[ei.a].id, mesh.vertices[ei.b].id};
            iss.hasMetric = true;
            iss.metricName = "incident_face_count";
            iss.metricValue = static_cast<double>(incidences);
            iss.message = "edge (" + mesh.vertices[ei.a].id + ", " + mesh.vertices[ei.b].id +
                          ") is shared by " + std::to_string(incidences) +
                          (sameDirectionPair ? " faces with inconsistent directions;" : " faces;") +
                          " a manifold edge must be traversed once in each direction by exactly 2 faces";
            rep.topologicalIssues.push_back(std::move(iss));
            if (sameDirectionPair) ++totalOrientationConflicts;
        }
    }

    // 边界边列表（端点索引升序）
    for (uint64_t key : boundaryEdgeKeys) {
        const EdgeInfo& ei = edgeMap.at(key);
        rep.boundaryEdges.emplace_back(mesh.vertices[ei.a].id, mesh.vertices[ei.b].id);
    }

    // ---- 孤立点：不被任何几何有效面引用 ----
    std::vector<bool> usedByValidFace(nv, false);
    for (int fi : validFaces)
        for (int vi : mesh.faces[fi].v) usedByValidFace[vi] = true;
    for (int i = 0; i < nv; ++i) {
        if (!usedByValidFace[i]) {
            rep.isolatedVertices.push_back(mesh.vertices[i].id);
            ++rep.isolatedVertexCount;
            Issue iss;
            iss.category = TOPO;
            iss.type = "isolated_vertex";
            iss.vertexIds.push_back(mesh.vertices[i].id);
            iss.message = "vertex '" + mesh.vertices[i].id +
                          "' is not referenced by any non-degenerate face";
            rep.topologicalIssues.push_back(std::move(iss));
        }
    }

    // ---- 连通分量：在有效面的顶点邻接关系上做并查集 ----
    DSU compDSU(nv);
    std::vector<bool> inComponent(nv, false);
    for (int fi : validFaces) {
        const auto& v = mesh.faces[fi].v;
        compDSU.unite(v[0], v[1]);
        compDSU.unite(v[1], v[2]);
        inComponent[v[0]] = inComponent[v[1]] = inComponent[v[2]] = true;
    }

    std::map<int, ComponentStats> statsByRoot;
    for (int i = 0; i < nv; ++i) {
        if (inComponent[i]) ++statsByRoot[compDSU.find(i)].vertexCount;
    }
    for (int fi : validFaces) {
        ++statsByRoot[compDSU.find(mesh.faces[fi].v[0])].faceCount;
    }
    for (auto& kv : edgeMap) {
        const EdgeInfo& ei = kv.second;
        int root = compDSU.find(ei.a);
        ComponentStats& st = statsByRoot[root];
        switch (edgeKind[kv.first]) {
            case EdgeKind::Boundary:
                ++st.boundaryEdgeCount;
                break;
            case EdgeKind::NonManifold: {
                ++st.nonManifoldEdgeCount;
                if (ei.forwardFaces.size() >= 2 || ei.backwardFaces.size() >= 2)
                    ++st.orientationConflictCount;
                break;
            }
            case EdgeKind::Manifold:
                break;
        }
    }

    int componentId = 1;
    for (auto& kv : statsByRoot) {
        ComponentStats& st = kv.second;
        st.componentId = componentId++;
        st.watertight = st.boundaryEdgeCount == 0 && st.nonManifoldEdgeCount == 0;
        st.orientationConsistent = st.orientationConflictCount == 0;
        rep.components.push_back(st);
    }

    // ---- 汇总结论 ----
    bool globalNoBoundary = boundaryEdgeKeys.empty();
    rep.watertight = !validFaces.empty() && globalNoBoundary && rep.nonManifoldEdgeCount == 0;
    // 方向冲突：非流形边中存在两个面同向遍历的数量。
    rep.orientationConflictCount = totalOrientationConflicts;
    rep.valid = rep.geometricIssues.empty() && rep.topologicalIssues.empty();

    return rep;
}

Value reportToJson(const Mesh& mesh, const ValidationReport& rep) {
    Value root = Value::object();
    root.set("status", Value(rep.valid ? "valid" : "errors"));
    root.set("coordinateSystem", Value(mesh.config.coordinateSystem));
    root.set("lengthUnit", Value(mesh.config.lengthUnit));

    Value precision = Value::object();
    precision.set("floating_point", Value("IEEE-754 double (53-bit mantissa)"));
    precision.set("output_format", Value("%.17g, round-trip exact"));
    precision.set("eps_position", Value(mesh.config.eps));
    precision.set("eps_area", Value(mesh.config.areaEps));
    root.set("precision", precision);

    Value summary = Value::object();
    int validFaceCount = 0;
    for (const auto& st : rep.components) validFaceCount += st.faceCount;
    summary.set("vertex_count", Value(rep.vertexCount));
    summary.set("face_count", Value(rep.faceCount));
    summary.set("non_degenerate_face_count", Value(validFaceCount));
    summary.set("geometric_degeneracy_count", Value(static_cast<int>(rep.geometricIssues.size())));
    summary.set("topological_error_count", Value(static_cast<int>(rep.topologicalIssues.size())));
    summary.set("nonmanifold_edge_count", Value(rep.nonManifoldEdgeCount));
    {
        int dupGroups = 0;
        for (const auto& iss : rep.topologicalIssues)
            if (iss.type == "duplicate_face") ++dupGroups;
        summary.set("duplicate_face_groups", Value(dupGroups));
    }
    summary.set("duplicate_face_extra", Value(rep.duplicateFaceExtraCount));
    summary.set("orientation_conflict_count", Value(rep.orientationConflictCount));
    summary.set("boundary_edge_count", Value(static_cast<int>(rep.boundaryEdges.size())));
    summary.set("isolated_vertex_count", Value(rep.isolatedVertexCount));
    summary.set("connected_component_count", Value(static_cast<int>(rep.components.size())));
    summary.set("watertight", Value(rep.watertight));
    {
        bool globalOrient = true;
        for (const auto& c : rep.components)
            globalOrient = globalOrient && c.orientationConsistent;
        summary.set("orientation_consistent", Value(globalOrient));
    }
    summary.set("valid", Value(rep.valid));
    root.set("summary", summary);

    Value geo = Value::array();
    for (const auto& iss : rep.geometricIssues) geo.push(issueToJson(iss));
    root.set("geometric_degeneracies", geo);

    Value topo = Value::array();
    for (const auto& iss : rep.topologicalIssues) topo.push(issueToJson(iss));
    root.set("topological_errors", topo);

    Value boundary = Value::array();
    for (const auto& e : rep.boundaryEdges) {
        Value pair = Value::array();
        pair.push(Value(e.first));
        pair.push(Value(e.second));
        boundary.push(pair);
    }
    root.set("boundary_edges", boundary);

    Value isolated = Value::array();
    for (const auto& id : rep.isolatedVertices) isolated.push(Value(id));
    root.set("isolated_vertices", isolated);

    Value comps = Value::array();
    for (const auto& c : rep.components) {
        Value cv = Value::object();
        cv.set("component_id", Value(c.componentId));
        cv.set("face_count", Value(c.faceCount));
        cv.set("vertex_count", Value(c.vertexCount));
        cv.set("boundary_edge_count", Value(c.boundaryEdgeCount));
        cv.set("nonmanifold_edge_count", Value(c.nonManifoldEdgeCount));
        cv.set("orientation_conflict_count", Value(c.orientationConflictCount));
        cv.set("watertight", Value(c.watertight));
        cv.set("orientation_consistent", Value(c.orientationConsistent));
        comps.push(cv);
    }
    root.set("connected_components", comps);

    return root;
}

} // namespace gridtopo
