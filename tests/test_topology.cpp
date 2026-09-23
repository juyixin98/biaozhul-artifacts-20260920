// 拓扑校验核心逻辑的 C++ 断言式单元测试。
// 运行: build/gridtopo_tests （全部通过时静默退出，返回 0）

#include "json.hpp"
#include "mesh.hpp"
#include "topology.hpp"

#include <cassert>
#include <cmath>
#include <functional>
#include <iostream>
#include <string>
#include <vector>

using namespace gridtopo;

namespace {

int g_failures = 0;

#define CHECK(cond)                                                                       \
    do {                                                                                  \
        if (!(cond)) {                                                                    \
            std::cerr << "FAIL: " << #cond << " (" << __FILE__ << ":" << __LINE__ << ")\n"; \
            ++g_failures;                                                                 \
        }                                                                                 \
    } while (0)

Mesh makeMesh(const std::vector<std::array<double, 3>>& verts,
              const std::vector<std::array<int, 3>>& tris) {
    Mesh m;
    m.vertices.resize(verts.size());
    for (size_t i = 0; i < verts.size(); ++i) {
        m.vertices[i].id = "v" + std::to_string(i);
        m.vertices[i].xyz = verts[i];
    }
    m.faces.resize(tris.size());
    for (size_t i = 0; i < tris.size(); ++i) {
        m.faces[i].id = "f" + std::to_string(i);
        m.faces[i].v = tris[i];
    }
    return m;
}

int countType(const std::vector<Issue>& issues, const std::string& type) {
    int n = 0;
    for (const auto& iss : issues)
        if (iss.type == type) ++n;
    return n;
}

void testClosedTetrahedron() {
    // 正则风格四面体，外法向取向，封闭且一致
    Mesh m = makeMesh(
        {{0, 0, 0}, {1, 0, 0}, {0, 1, 0}, {0, 0, 1}},
        {{0, 2, 1}, {0, 1, 3}, {1, 2, 3}, {0, 3, 2}});
    ValidationReport r = validate(m);
    CHECK(r.valid);
    CHECK(r.geometricIssues.empty());
    CHECK(r.topologicalIssues.empty());
    CHECK(r.watertight);
    CHECK(r.boundaryEdges.empty());
    CHECK(r.components.size() == 1);
    CHECK(r.components[0].faceCount == 4);
    CHECK(r.components[0].vertexCount == 4);
    CHECK(r.components[0].boundaryEdgeCount == 0);
    CHECK(r.components[0].orientationConsistent);
    CHECK(r.isolatedVertices.empty());
}

void testOpenHoleSurface() {
    // 四面体去掉一个面：3 个面、6 条边中 3 条为边界边
    Mesh m = makeMesh(
        {{0, 0, 0}, {1, 0, 0}, {0, 1, 0}, {0, 0, 1}},
        {{0, 2, 1}, {0, 1, 3}, {1, 2, 3}});
    ValidationReport r = validate(m);
    CHECK(r.valid); // 开曲面没有错误，只是不封闭
    CHECK(r.watertight == false);
    CHECK(r.boundaryEdges.size() == 3);
    CHECK(r.components.size() == 1);
    CHECK(r.components[0].boundaryEdgeCount == 3);
    CHECK(r.components[0].watertight == false);
}

void testThreeFacesOneEdge() {
    // 三个面共享边 (v0,v1)
    Mesh m = makeMesh(
        {{0, 0, 0}, {1, 0, 0}, {0, 1, 0}, {0, 0, 1}, {0, -1, 1}},
        {{0, 1, 2}, {1, 0, 3}, {0, 1, 4}});
    ValidationReport r = validate(m);
    CHECK(countType(r.topologicalIssues, "nonmanifold_edge") == 1);
    CHECK(r.nonManifoldEdgeCount == 1);
    const Issue* iss = nullptr;
    for (const auto& i : r.topologicalIssues)
        if (i.type == "nonmanifold_edge") iss = &i;
    CHECK(iss != nullptr);
    CHECK(iss->faceIds.size() == 3);
    // 报告中必须给出三个面的 ID
    bool idsOk = iss->faceIds[0] == "f0" && iss->faceIds[1] == "f1" && iss->faceIds[2] == "f2";
    CHECK(idsOk);
}

void testDuplicateFacesOppositeWinding() {
    // 同一三角形出现两次，绕序相反：内部正确共享，但属于重复面
    Mesh m = makeMesh({{0, 0, 0}, {1, 0, 0}, {0, 1, 0}},
                      {{0, 1, 2}, {0, 2, 1}});
    ValidationReport r = validate(m);
    CHECK(countType(r.topologicalIssues, "duplicate_face") == 1);
    CHECK(r.nonManifoldEdgeCount == 0);
    CHECK(r.watertight); // 每条边恰好两入射
    const Issue* iss = nullptr;
    for (const auto& i : r.topologicalIssues)
        if (i.type == "duplicate_face") iss = &i;
    CHECK(iss != nullptr && iss->detail == "opposite_winding");
    CHECK(r.valid == false);
}

void testDuplicateFacesSameWinding() {
    // 同绕序重复：每条边三入射 → 非流形边
    Mesh m = makeMesh({{0, 0, 0}, {1, 0, 0}, {0, 1, 0}},
                      {{0, 1, 2}, {0, 1, 2}});
    ValidationReport r = validate(m);
    CHECK(countType(r.topologicalIssues, "duplicate_face") == 1);
    CHECK(r.nonManifoldEdgeCount == 3);
}

void testOrientationConflict() {
    // f0: 0->1->2, f1: 0->1->3，两面对共享边 (v0,v1) 同向遍历。
    // 该边入射为 2 个同向半边，属于非流形边（无法在该边上一致定向）。
    Mesh m = makeMesh(
        {{0, 0, 0}, {1, 0, 0}, {0, 1, 0}, {0, -1, 0}},
        {{0, 1, 2}, {0, 1, 3}});
    ValidationReport r = validate(m);
    CHECK(countType(r.topologicalIssues, "nonmanifold_edge") == 1);
    const Issue* iss = nullptr;
    for (const auto& i : r.topologicalIssues)
        if (i.type == "nonmanifold_edge") iss = &i;
    CHECK(iss != nullptr);
    CHECK(iss->faceIds.size() == 2);
    CHECK(iss->faceIds[0] == "f0" && iss->faceIds[1] == "f1");
    // 其余四条边为边界边
    CHECK(r.boundaryEdges.size() == 4);
    CHECK(r.orientationConflictCount == 1);
    CHECK(r.components[0].orientationConsistent == false);
}

void testIsolatedVertex() {
    Mesh m = makeMesh(
        {{0, 0, 0}, {1, 0, 0}, {0, 1, 0}, {5, 5, 5}},
        {{0, 1, 2}});
    ValidationReport r = validate(m);
    CHECK(countType(r.topologicalIssues, "isolated_vertex") == 1);
    CHECK(r.isolatedVertices.size() == 1);
    CHECK(r.isolatedVertices[0] == "v3");
    CHECK(r.components.size() == 1);
    CHECK(r.components[0].vertexCount == 3);
}

void testTwoDisconnectedComponents() {
    Mesh m = makeMesh(
        {{0, 0, 0}, {1, 0, 0}, {0, 1, 0},
         {0, 0, 5}, {1, 0, 5}, {0, 1, 5}},
        {{0, 1, 2}, {3, 4, 5}});
    ValidationReport r = validate(m);
    CHECK(r.components.size() == 2);
    CHECK(r.components[0].faceCount == 1 && r.components[1].faceCount == 1);
    CHECK(r.components[0].boundaryEdgeCount == 3);
}

void testZeroAreaTriangle() {
    Mesh m = makeMesh(
        {{0, 0, 0}, {1, 0, 0}, {2, 0, 0}},
        {{0, 1, 2}});
    ValidationReport r = validate(m);
    CHECK(countType(r.geometricIssues, "zero_area_triangle") == 1);
    // 退化面剔除后没有有效面
    CHECK(r.components.empty());
    CHECK(r.isolatedVertices.size() == 3);
    const Issue* iss = nullptr;
    for (const auto& i : r.geometricIssues)
        if (i.type == "zero_area_triangle") iss = &i;
    CHECK(iss != nullptr);
    CHECK(iss->hasMetric && iss->metricName == "area");
    CHECK(std::fabs(iss->metricValue) < 1e-15);
}

void testRepeatedVertexIndex() {
    Mesh m = makeMesh({{0, 0, 0}, {1, 0, 0}},
                      {{0, 1, 1}});
    ValidationReport r = validate(m);
    CHECK(countType(r.geometricIssues, "repeated_vertex_index") == 1);
}

void testCoincidentVertices() {
    Mesh m = makeMesh(
        {{0, 0, 0}, {1e-12, 0, 0}, {1, 0, 0}, {0, 1, 0}},
        {{0, 2, 3}});
    ValidationReport r = validate(m);
    CHECK(countType(r.geometricIssues, "coincident_vertices") == 1);
    // 索引 1 的顶点不被有效面引用 → 孤立点（拓扑仍用原始索引，不自动合并）
    CHECK(countType(r.topologicalIssues, "isolated_vertex") == 1);
}

void testNonFiniteCoordinate() {
    Mesh m = makeMesh(
        {{0, 0, 0}, {1, 0, 0}, {0, 1, 0}},
        {{0, 1, 2}});
    m.vertices[1].xyz[0] = std::nan("");
    ValidationReport r = validate(m);
    CHECK(countType(r.geometricIssues, "nonfinite_coordinate") == 1);
    CHECK(countType(r.geometricIssues, "face_uses_nonfinite_vertex") == 1);
}

void testJsonRoundTripPreservesIds() {
    // 通过 JSON 请求入口加载，确认自定义 id 被保留到报告
    const char* req = R"({
        "vertices": [
            {"id": "A", "coordinates": [0,0,0]},
            {"id": "B", "coordinates": [1,0,0]},
            {"id": "C", "coordinates": [0,1,0]}
        ],
        "faces": [ {"id": "tri1", "vertices": ["A","B","C"]} ]
    })";
    json::Value root = json::parse(req);
    Mesh m = loadMesh(root);
    ValidationReport r = validate(m);
    json::Value jv = reportToJson(m, r);
    CHECK(jv.get("summary").get("face_count").numberValue == 1);
    CHECK(jv.get("connected_components").arrayValue[0]
              .get("boundary_edge_count").numberValue == 3);
    const auto& edges = jv.get("boundary_edges").arrayValue;
    bool foundA = false, foundB = false;
    for (const auto& e : edges) {
        if (e.arrayValue[0].stringValue == "A" || e.arrayValue[1].stringValue == "A") foundA = true;
        if (e.arrayValue[0].stringValue == "B" || e.arrayValue[1].stringValue == "B") foundB = true;
    }
    CHECK(foundA && foundB);
}

} // namespace

int main() {
    std::vector<std::pair<std::string, std::function<void()>>> tests = {
        {"closed_tetrahedron", testClosedTetrahedron},
        {"open_hole_surface", testOpenHoleSurface},
        {"three_faces_one_edge", testThreeFacesOneEdge},
        {"duplicate_faces_opposite_winding", testDuplicateFacesOppositeWinding},
        {"duplicate_faces_same_winding", testDuplicateFacesSameWinding},
        {"orientation_conflict", testOrientationConflict},
        {"isolated_vertex", testIsolatedVertex},
        {"two_disconnected_components", testTwoDisconnectedComponents},
        {"zero_area_triangle", testZeroAreaTriangle},
        {"repeated_vertex_index", testRepeatedVertexIndex},
        {"coincident_vertices", testCoincidentVertices},
        {"nonfinite_coordinate", testNonFiniteCoordinate},
        {"json_roundtrip_preserves_ids", testJsonRoundTripPreservesIds},
    };
    for (const auto& [name, fn] : tests) {
        int before = g_failures;
        fn();
        if (g_failures == before) std::cout << "PASS " << name << "\n";
        else std::cout << "FAIL " << name << "\n";
    }
    if (g_failures == 0) {
        std::cout << "All " << tests.size() << " test groups passed.\n";
        return 0;
    }
    std::cerr << g_failures << " assertion(s) failed.\n";
    return 1;
}
