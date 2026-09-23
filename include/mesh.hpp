#ifndef GRIDTOPO_MESH_HPP
#define GRIDTOPO_MESH_HPP

#include "json.hpp"

#include <array>
#include <string>
#include <vector>

namespace gridtopo {

// 请求中的精度与坐标系配置。
struct Config {
    std::string coordinateSystem = "right-handed Cartesian";
    std::string lengthUnit = "unitless";
    // 位置重合容差（绝对，单位同坐标）。
    double eps = 1e-9;
    // 退化面积容差：|叉积|/2 <= areaEps 视为零面积。
    double areaEps = 1e-12;
};

struct Vertex {
    std::string id;
    std::array<double, 3> xyz{0.0, 0.0, 0.0};
};

struct Face {
    std::string id;
    // 始终存储三个顶点索引；重复顶点时索引可相同。
    std::array<int, 3> v{-1, -1, -1};
};

struct Mesh {
    Config config;
    std::vector<Vertex> vertices;
    std::vector<Face> faces;
};

// 读取 JSON 请求并构造网格。
// vertices: [[x,y,z], ...] 或 [{"id":..,"coordinates":[x,y,z]}, ...]
// faces:    [[i,j,k], ...] 或 [{"id":..,"vertices":[id|index,...]}, ...]
// 输入本身非法（JSON 语法、越界引用、字段类型错误等）抛出 std::runtime_error，
// 调用方以请求级错误（exit code 2）返回。
Mesh loadMesh(const json::Value& root);

} // namespace gridtopo

#endif // GRIDTOPO_MESH_HPP
