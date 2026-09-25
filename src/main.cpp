// main.cpp — 空间最近邻索引后端：JSON 请求入口（离线、纯计算、无前端）
//
// 用法:
//   ./spatial_index [请求文件.json] [-o 输出文件.json]
//   不带文件名时从标准输入读取；不指定 -o 时结果写到标准输出。
//
// 请求格式见 examples/ 与 README。成功时退出码 0；请求非法或 IO 失败时
// 向输出写入 {"ok":false,...} 并以退出码 1 结束。
#include <chrono>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <set>
#include <sstream>
#include <string>
#include <vector>

#include "json.hpp"
#include "spatial.hpp"

using sjson::Json;
using spatial::KdTree;
using spatial::Point;
using spatial::XY;

namespace {

constexpr long double COORD_LIMIT = 1e2400L;  // 文档化的坐标/半径安全上限

struct RequestError {
    std::string code;
    std::string message;
};

Json errorResponse(const std::string& code, const std::string& message) {
    Json root = Json::object();
    Json err = Json::object();
    err.obj["code"] = Json::string(code);
    err.obj["message"] = Json::string(message);
    root.obj["ok"] = Json::boolean(false);
    root.obj["error"] = err;
    return root;
}

// 取出要求存在、且类型为 number 的字段。
bool requireNumber(const Json& parent, const char* field, long double& out,
                   std::string& err) {
    const Json* v = parent.find(field);
    if (!v) { err = std::string("缺少字段 \"") + field + "\""; return false; }
    if (!v->isNumber()) { err = std::string("字段 \"") + field + "\" 必须是数字"; return false; }
    out = v->numValue;
    return true;
}

bool finiteCoord(long double v) { return std::isfinite(v) && std::fabs(v) <= COORD_LIMIT; }

void parsePoints(const Json& req, std::vector<Point>& points, RequestError& re) {
    const Json* jp = req.find("points");
    if (!jp) { re = {"MISSING_FIELD", "缺少顶层字段 \"points\""}; return; }
    if (!jp->isArray()) { re = {"INVALID_TYPE", "\"points\" 必须是数组"}; return; }

    points.reserve(jp->arr.size());
    std::set<spatial::Id> ids;
    for (size_t i = 0; i < jp->arr.size(); ++i) {
        const Json& j = jp->arr[i];
        std::string prefix = "points[" + std::to_string(i) + "] ";
        if (!j.isObject()) { re = {"INVALID_TYPE", prefix + "必须是对象"}; return; }

        const Json* jid = j.find("id");
        if (!jid) { re = {"MISSING_FIELD", prefix + "缺少 \"id\""}; return; }
        if (!jid->isNumber() || !jid->isInteger) {
            re = {"INVALID_TYPE", prefix + "\"id\" 必须是整数"};
            return;
        }
        if (!ids.insert(jid->intValue).second) {
            re = {"DUPLICATE_ID", prefix + "存在重复的 id: " + std::to_string(jid->intValue)};
            return;
        }

        Point p;
        p.id = jid->intValue;
        std::string err;
        if (!requireNumber(j, "x", p.x, err) || !requireNumber(j, "y", p.y, err)) {
            re = {"INVALID_TYPE", prefix + err};
            return;
        }
        if (!finiteCoord(p.x) || !finiteCoord(p.y)) {
            re = {"VALUE_OUT_OF_RANGE",
                  prefix + "x/y 必须是绝对值不超过 1e2400 的有限数（见 README 数值约定）"};
            return;
        }
        points.push_back(p);
    }
}

Json candidateJson(const spatial::Candidate& c) {
    Json j = Json::object();
    j.obj["id"] = Json::integer(c.id);
    j.obj["x"] = Json::number(c.x);
    j.obj["y"] = Json::number(c.y);
    j.obj["distance_sq"] = Json::number(c.d2);
    long double d = std::sqrt(c.d2);
    j.obj["distance"] = Json::number(std::isfinite(d) ? d : c.d2);
    return j;
}

Json runQueries(const Json& req, const KdTree& tree, RequestError& re,
                long long& totalQueryUs) {
    const Json* jq = req.find("queries");
    if (!jq) { re = {"MISSING_FIELD", "缺少顶层字段 \"queries\""}; return Json::null(); }
    if (!jq->isArray()) { re = {"INVALID_TYPE", "\"queries\" 必须是数组"}; return Json::null(); }

    Json results = Json::array();
    auto t0 = std::chrono::steady_clock::now();

    for (size_t i = 0; i < jq->arr.size(); ++i) {
        const Json& q = jq->arr[i];
        std::string prefix = "queries[" + std::to_string(i) + "] ";
        if (!q.isObject()) { re = {"INVALID_TYPE", prefix + "必须是对象"}; return Json::null(); }

        const Json* jt = q.find("type");
        if (!jt || !jt->isString()) {
            re = {"MISSING_FIELD", prefix + "缺少字符串字段 \"type\" (knn|radius)"};
            return Json::null();
        }
        XY center;
        std::string err;
        if (!requireNumber(q, "x", center.x, err) || !requireNumber(q, "y", center.y, err)) {
            re = {"INVALID_TYPE", prefix + err};
            return Json::null();
        }
        if (!finiteCoord(center.x) || !finiteCoord(center.y)) {
            re = {"VALUE_OUT_OF_RANGE", prefix + "x/y 超出安全范围"};
            return Json::null();
        }

        Json item = Json::object();

        if (jt->strValue == "knn") {
            const Json* jk = q.find("k");
            if (!jk) { re = {"MISSING_FIELD", prefix + "knn 查询缺少 \"k\""}; return Json::null(); }
            if (!jk->isNumber() || !jk->isInteger || jk->intValue < 0) {
                re = {"INVALID_TYPE", prefix + "\"k\" 必须是非负整数"};
                return Json::null();
            }
            if (jk->numValue > static_cast<long double>(SIZE_MAX)) {
                re = {"VALUE_OUT_OF_RANGE", prefix + "\"k\" 过大"};
                return Json::null();
            }
            std::size_t k = static_cast<std::size_t>(jk->intValue);
            bool truncated = false;
            std::vector<spatial::Candidate> hits = tree.kNearest(center, k, &truncated);

            item.obj["type"] = Json::string("knn");
            item.obj["k"] = Json::integer(jk->intValue);
            item.obj["truncated"] = Json::boolean(truncated);
            item.obj["count"] = Json::integer(static_cast<std::int64_t>(hits.size()));
            Json arr = Json::array();
            for (const auto& c : hits) arr.arr.push_back(candidateJson(c));
            item.obj["results"] = arr;
        } else if (jt->strValue == "radius") {
            const Json* jr = q.find("radius");
            if (!jr) { re = {"MISSING_FIELD", prefix + "radius 查询缺少 \"radius\""}; return Json::null(); }
            if (!jr->isNumber()) { re = {"INVALID_TYPE", prefix + "\"radius\" 必须是数字"}; return Json::null(); }
            long double radius = jr->numValue;
            if (!std::isfinite(radius) || radius < 0.0L || radius > COORD_LIMIT) {
                re = {"VALUE_OUT_OF_RANGE",
                      prefix + "\"radius\" 必须是 [0, 1e2400] 内的有限数"};
                return Json::null();
            }
            std::vector<spatial::Candidate> hits;
            if (!tree.radiusQuery(center, radius, hits)) {
                re = {"INVALID_TYPE", prefix + "半径查询执行失败"};
                return Json::null();
            }
            item.obj["type"] = Json::string("radius");
            item.obj["radius"] = Json::number(radius);
            item.obj["count"] = Json::integer(static_cast<std::int64_t>(hits.size()));
            Json arr = Json::array();
            for (const auto& c : hits) arr.arr.push_back(candidateJson(c));
            item.obj["results"] = arr;
        } else {
            re = {"INVALID_TYPE", prefix + "未知 type \"" + jt->strValue + "\"，应为 knn 或 radius"};
            return Json::null();
        }
        results.arr.push_back(item);
    }

    auto t1 = std::chrono::steady_clock::now();
    totalQueryUs = std::chrono::duration_cast<std::chrono::microseconds>(t1 - t0).count();
    return results;
}

void printUsage(const char* prog) {
    std::cerr <<
        "用法: " << prog << " [请求文件.json] [-o 输出文件.json]\n"
        "  不带文件名时从标准输入读取 JSON 请求；不指定 -o 时结果写到标准输出。\n"
        "  请求/响应格式见 README.md 与 examples/ 目录。\n";
}

}  // namespace

int main(int argc, char** argv) {
    std::string inPath;
    std::string outPath;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "-h" || a == "--help") { printUsage(argv[0]); return 0; }
        if (a == "-o") {
            if (i + 1 >= argc) { std::cerr << "-o 后需要输出文件路径\n"; return 2; }
            outPath = argv[++i];
        } else if (!inPath.empty()) {
            std::cerr << "多余的参数: " << a << "\n";
            printUsage(argv[0]);
            return 2;
        } else {
            inPath = a;
        }
    }

    // 1) 读取输入
    std::string input;
    if (inPath.empty()) {
        std::ostringstream ss;
        ss << std::cin.rdbuf();
        input = ss.str();
        if (!std::cin.good() && !std::cin.eof()) {
            std::cerr << "读取标准输入失败\n";
            return 1;
        }
    } else {
        std::ifstream f(inPath, std::ios::binary);
        if (!f) { std::cerr << "无法打开输入文件: " << inPath << "\n"; return 1; }
        std::ostringstream ss;
        ss << f.rdbuf();
        input = ss.str();
    }

    // 2) 解析 JSON
    Json req;
    std::string parseErr;
    if (!Json::parse(input, req, parseErr)) {
        Json resp = errorResponse("INVALID_JSON", parseErr);
        std::cout << resp.dumpPretty() << "\n";
        return 1;
    }
    if (!req.isObject()) {
        Json resp = errorResponse("INVALID_TYPE", "请求根必须是 JSON 对象");
        std::cout << resp.dumpPretty() << "\n";
        return 1;
    }

    // 3) 校验并读取点集
    std::vector<Point> points;
    RequestError re;
    parsePoints(req, points, re);
    if (!re.code.empty()) {
        std::cout << errorResponse(re.code, re.message).dumpPretty() << "\n";
        return 1;
    }

    // 4) 构建静态 KD 树
    auto tb0 = std::chrono::steady_clock::now();
    KdTree tree(std::move(points));
    auto tb1 = std::chrono::steady_clock::now();
    long long buildUs = std::chrono::duration_cast<std::chrono::microseconds>(tb1 - tb0).count();

    // 5) 执行查询
    long long queryUs = 0;
    Json results = runQueries(req, tree, re, queryUs);
    if (!re.code.empty()) {
        std::cout << errorResponse(re.code, re.message).dumpPretty() << "\n";
        return 1;
    }

    // 6) 组装成功响应
    Json resp = Json::object();
    resp.obj["ok"] = Json::boolean(true);
    Json summary = Json::object();
    summary.obj["points"] = Json::integer(static_cast<std::int64_t>(tree.size()));
    summary.obj["queries"] = Json::integer(static_cast<std::int64_t>(results.arr.size()));
    summary.obj["build_time_us"] = Json::integer(buildUs);
    summary.obj["query_time_us"] = Json::integer(queryUs);
    resp.obj["summary"] = summary;
    resp.obj["results"] = results;

    std::string outText = resp.dumpPretty() + "\n";
    if (outPath.empty()) {
        std::cout << outText;
    } else {
        std::ofstream f(outPath, std::ios::binary);
        if (!f) { std::cerr << "无法打开输出文件: " << outPath << "\n"; return 1; }
        f << outText;
        if (!f) { std::cerr << "写入输出文件失败: " << outPath << "\n"; return 1; }
    }
    return 0;
}
