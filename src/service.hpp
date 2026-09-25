// service.hpp — JSON 请求处理（distance / range）与数据集加载
#pragma once

#include <string>
#include <vector>

#include "geo.hpp"
#include "json.hpp"

namespace svc {

struct Point {
    std::string id;
    sph::GeoPoint geo;
};

struct LoadResult {
    bool ok = false;
    std::string error;
    std::vector<Point> points;
};

// 从 JSON 文本加载点集。期望：{ "points": [ {"id": "...", "lat": .., "lon": ..}, ... ] }
// id 可省略（用序号填充）。
LoadResult loadPointsJson(const std::string& text);

// 从文件路径加载点集（文件不存在/不可读返回错误）
LoadResult loadPointsFile(const std::string& path);

// 处理一个请求 JSON 文本，返回响应 JSON 文本与 HTTP 风格状态码
//   200 成功；400 请求错误（格式/字段/坐标非法）；500 内部错误
struct Response {
    int status = 200;
    std::string body;
};

Response handleRequest(const std::string& requestText);

}  // namespace svc
