// json.hpp - 最小 JSON 解析与序列化(无第三方依赖)
#pragma once

#include <map>
#include <memory>
#include <string>
#include <vector>

namespace json {

enum class Type { Null, Bool, Number, String, Array, Object };

class Value {
public:
    Type type = Type::Null;
    bool b = false;
    double num = 0.0;
    std::string str;
    std::vector<Value> arr;
    std::map<std::string, Value> obj;  // 对象按键排序输出, 结果稳定

    Value() = default;
    static Value makeBool(bool v) {
        Value x;
        x.type = Type::Bool;
        x.b = v;
        return x;
    }
    static Value makeNum(double v) {
        Value x;
        x.type = Type::Number;
        x.num = v;
        return x;
    }
    static Value makeStr(std::string v) {
        Value x;
        x.type = Type::String;
        x.str = std::move(v);
        return x;
    }
    static Value makeArr() {
        Value x;
        x.type = Type::Array;
        return x;
    }
    static Value makeObj() {
        Value x;
        x.type = Type::Object;
        return x;
    }

    const Value* find(const std::string& key) const {
        auto it = obj.find(key);
        return it == obj.end() ? nullptr : &it->second;
    }
    void set(const std::string& key, Value v) { obj[key] = std::move(v); }
};

struct ParseResult {
    bool ok = false;
    std::string error;
    size_t line = 0;
    size_t col = 0;
    Value value;
};

ParseResult parse(const std::string& text);

// 以 2 空格缩进序列化。数字使用 %.17g 保证 double 往返精度。
std::string dump(const Value& v, int indent = 2);

}  // namespace json
