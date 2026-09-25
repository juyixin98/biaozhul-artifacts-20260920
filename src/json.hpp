// json.hpp — 极简 JSON 解析/序列化（无第三方依赖，够用即可）
// 支持：object / array / string / number(double) / bool / null
// 数字统一以 double 存储（IEEE-754 与本项目经纬度精度匹配）。
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace json {

enum class Type { Null, Bool, Number, String, Array, Object };

class Value {
public:
    Type type = Type::Null;
    bool boolean = false;
    double number = 0.0;
    std::string str;
    std::vector<Value> arr;
    std::map<std::string, Value> obj;  // 有序 map，输出稳定

    Value() = default;
    static Value makeObject() { Value v; v.type = Type::Object; return v; }
    static Value makeArray() { Value v; v.type = Type::Array; return v; }
    static Value makeNumber(double d) { Value v; v.type = Type::Number; v.number = d; return v; }
    static Value makeString(std::string s) { Value v; v.type = Type::String; v.str = std::move(s); return v; }
    static Value makeBool(bool b) { Value v; v.type = Type::Bool; v.boolean = b; return v; }

    bool isObject() const { return type == Type::Object; }
    bool isArray() const { return type == Type::Array; }
    bool isNumber() const { return type == Type::Number; }
    bool isString() const { return type == Type::String; }

    // 对象取字段（不存在返回 nullptr）
    const Value* find(const std::string& key) const {
        if (!isObject()) return nullptr;
        auto it = obj.find(key);
        return it == obj.end() ? nullptr : &it->second;
    }
};

struct ParseResult {
    bool ok = false;
    Value value;
    std::string error;  // ok=false 时给出位置与原因
};

ParseResult parse(const std::string& text);

// 序列化。indent>=0 时美化输出（2 空格缩进）。
std::string dump(const Value& v, int indent = 2);

}  // namespace json
