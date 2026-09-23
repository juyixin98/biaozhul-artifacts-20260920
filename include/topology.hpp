#ifndef GRIDTOPO_TOPOLOGY_HPP
#define GRIDTOPO_TOPOLOGY_HPP

#include "json.hpp"
#include "mesh.hpp"

#include <string>
#include <vector>

namespace gridtopo {

// 单个校验发现。几何退化与拓扑错误严格分离（category 字段）。
struct Issue {
    // "geometric_degeneracy" 或 "topological_error"
    std::string category;
    // 机器可读类型，如 nonmanifold_edge / duplicate_face / zero_area_triangle
    std::string type;
    std::string message;
    std::vector<std::string> faceIds;
    std::vector<std::string> vertexIds;
    // 可选数值负载（面积、距离、入射面数等）
    bool hasMetric = false;
    std::string metricName;
    double metricValue = 0.0;
    // 重复面专用：same_winding / opposite_winding / mixed_winding
    std::string detail;
};

struct ComponentStats {
    int componentId = 0;
    int faceCount = 0;
    int vertexCount = 0;
    int boundaryEdgeCount = 0;
    int nonManifoldEdgeCount = 0;
    int orientationConflictCount = 0;
    // 封闭：每条边恰好被两个面共享（无边界边、无非流形边）
    bool watertight = false;
    // 取向一致：不存在两个面同向遍历同一条边
    bool orientationConsistent = false;
};

struct ValidationReport {
    int vertexCount = 0;
    int faceCount = 0;

    std::vector<Issue> geometricIssues;
    std::vector<Issue> topologicalIssues;

    // 每个元素为一对端点 id（端点索引升序）
    std::vector<std::pair<std::string, std::string>> boundaryEdges;
    std::vector<std::string> isolatedVertices;

    std::vector<ComponentStats> components;

    int nonManifoldEdgeCount = 0;
    int duplicateFaceExtraCount = 0;
    int orientationConflictCount = 0;
    int isolatedVertexCount = 0;

    bool watertight = false;
    // valid：无几何退化且无拓扑错误（边界边是开曲面的正常属性，不算错误）
    bool valid = false;
};

// 执行全部检查。纯函数：输入网格，输出报告。
ValidationReport validate(const Mesh& mesh);

// 报告序列化为 JSON（坐标与数值，无图形内容）。
json::Value reportToJson(const Mesh& mesh, const ValidationReport& report);

} // namespace gridtopo

#endif // GRIDTOPO_TOPOLOGY_HPP
