#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace json {

enum class Type { Null, Bool, Number, String, Array, Object };

struct Value {
    Type type = Type::Null;
    bool boolean = false;
    long double number = 0.0L;
    std::string str; // 已解码为 UTF-8
    std::vector<Value> arr;
    // 对象同时保留有序插入（保证输出顺序稳定）与按名查找。
    std::vector<std::pair<std::string, Value>> obj;
    std::map<std::string, std::size_t> objIndex;

    const Value* find(const std::string& key) const;
    bool is(Type t) const { return type == t; }
};

// 解析严格 JSON；失败返回 nullptr 并填写 error。
// 重复键：后出现的键覆盖按名查找结果（保留顺序槽位不变）。
std::unique_ptr<Value> parse(const std::string& text, std::string* error);

} // namespace json
