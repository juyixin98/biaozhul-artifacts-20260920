// main.cpp — 扫描线矩形并面积/周长服务的 JSON 请求入口
//
// 用法：
//   rectunion                 从标准输入读取请求 JSON
//   rectunion <request.json>  从文件读取请求 JSON
//
// 请求（整数坐标；字段名 x1,y1,x2,y2）：
//   { "rectangles": [ {"x1":0,"y1":0,"x2":2,"y2":2}, ... ] }
//
// 响应（结果为精确十进制整数，以 JSON number 输出）：
//   { "area": "4", "perimeter": "8", "input_count": 1, "nondegenerate_count": 1 }
//
// 结果可能超过 64 位整数范围（最大约 2^128），故以字符串形式给出精确值，
// 避免任何 JSON 消费方按 double 解析时丢失精度。
#include <cstdint>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "geometry.hpp"
#include "json.hpp"

using namespace rectunion;

namespace {

struct ParseResult {
    std::vector<Rect> rects;
    long long inputCount = 0;
};

bool parseRect(const JsonValue& v, long long idx, Rect& out, bool& degenerate,
               std::string& err) {
    if (v.type != JsonValue::Obj) {
        err = "请求格式错误: rectangles[" + std::to_string(idx) + "] 必须是对象";
        return false;
    }
    const char* names[4] = {"x1", "y1", "x2", "y2"};
    long long* targets[4] = {&out.x1, &out.y1, &out.x2, &out.y2};
    for (int k = 0; k < 4; ++k) {
        const JsonValue* f = v.find(names[k]);
        if (!f) {
            err = std::string("请求格式错误: rectangles[") + std::to_string(idx) +
                  "] 缺少字段 " + names[k];
            return false;
        }
        if (!jsonInt64(*f, *targets[k])) {
            err = std::string("请求格式错误: rectangles[") + std::to_string(idx) +
                  "]." + names[k] + " 必须是 int64 范围内的整数";
            return false;
        }
    }
    if (out.x1 > out.x2 || out.y1 > out.y2) {
        err = std::string("请求格式错误: rectangles[") + std::to_string(idx) +
              "] 要求 x1 <= x2 且 y1 <= y2";
        return false;
    }
    degenerate = (out.x1 == out.x2 || out.y1 == out.y2);
    return true;
}

bool parseRequest(const JsonValue& root, ParseResult& pr, std::string& err) {
    if (root.type != JsonValue::Obj) {
        err = "请求格式错误: 根必须是 JSON 对象";
        return false;
    }
    const JsonValue* arr = root.find("rectangles");
    if (!arr) {
        err = "请求格式错误: 缺少字段 rectangles";
        return false;
    }
    if (arr->type != JsonValue::Arr) {
        err = "请求格式错误: rectangles 必须是数组";
        return false;
    }
    pr.inputCount = static_cast<long long>(arr->arr.size());
    pr.rects.reserve(arr->arr.size());
    for (long long i = 0; i < pr.inputCount; ++i) {
        Rect r{};
        bool degenerate = false;
        if (!parseRect(arr->arr[static_cast<size_t>(i)], i, r, degenerate, err))
            return false;
        if (!degenerate) pr.rects.push_back(r);
    }
    return true;
}

void emitError(const std::string& message) {
    std::cerr << "{\"error\":" << jsonEscape(message) << "}\n";
}

} // namespace

int main(int argc, char** argv) {
    std::string raw;
    if (argc >= 2) {
        std::ifstream in(argv[1], std::ios::binary);
        if (!in) {
            emitError(std::string("无法打开输入文件: ") + argv[1]);
            return 2;
        }
        std::ostringstream ss;
        ss << in.rdbuf();
        raw = ss.str();
    } else {
        std::ostringstream ss;
        ss << std::cin.rdbuf();
        raw = ss.str();
    }

    JsonValue root;
    std::string err;
    if (!parseJson(raw, root, err)) {
        emitError(err);
        return 1;
    }

    ParseResult pr;
    if (!parseRequest(root, pr, err)) {
        emitError(err);
        return 1;
    }

    Metrics m = unionMetrics(pr.rects);

    std::cout << "{"
              << "\"area\":" << jsonEscape(toString(m.area)) << ","
              << "\"perimeter\":" << jsonEscape(toString(m.perimeter)) << ","
              << "\"input_count\":" << pr.inputCount << ","
              << "\"nondegenerate_count\":" << pr.rects.size()
              << "}\n";
    return 0;
}
