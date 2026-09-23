#ifndef GRIDTOPO_JSON_HPP
#define GRIDTOPO_JSON_HPP

// 最小 JSON 值类型与解析/序列化实现（零第三方依赖）。
// 仅使用标准库；对象保持键的插入顺序（非字典序），便于报告稳定可读。

#include <map>
#include <string>
#include <vector>
#include <cstdint>

namespace json {

struct ParseError {
    std::string message;
    int line;
    int column;
};

class Value {
public:
    enum class Type { Null, Bool, Number, String, Array, Object };

    Type type = Type::Null;
    bool boolValue = false;
    double numberValue = 0.0;
    std::string stringValue;
    std::vector<Value> arrayValue;
    // 保持插入顺序的 JSON 对象：key 出现顺序 + 映射表
    std::vector<std::string> keys;
    std::map<std::string, Value> members;

    Value() = default;
    explicit Value(bool b) : type(Type::Bool), boolValue(b) {}
    explicit Value(double d) : type(Type::Number), numberValue(d) {}
    explicit Value(int i) : type(Type::Number), numberValue(static_cast<double>(i)) {}
    explicit Value(long long i) : type(Type::Number), numberValue(static_cast<double>(i)) {}
    explicit Value(std::string s) : type(Type::String), stringValue(std::move(s)) {}
    explicit Value(const char* s) : type(Type::String), stringValue(s) {}

    static Value array() {
        Value v;
        v.type = Type::Array;
        return v;
    }
    static Value object() {
        Value v;
        v.type = Type::Object;
        return v;
    }

    bool isObject() const { return type == Type::Object; }
    bool isArray() const { return type == Type::Array; }
    bool isNumber() const { return type == Type::Number; }
    bool isString() const { return type == Type::String; }
    bool isBool() const { return type == Type::Bool; }

    bool has(const std::string& key) const {
        return type == Type::Object && members.find(key) != members.end();
    }
    const Value& get(const std::string& key) const;
    Value& set(const std::string& key, Value v);

    void push(Value v) { arrayValue.push_back(std::move(v)); }
};

// 解析严格 JSON；失败抛出 ParseError。
Value parse(const std::string& text);

// 序列化为 2 空格缩进的 JSON 文本。
std::string dump(const Value& value, int indent = 2);

} // namespace json

#endif // GRIDTOPO_JSON_HPP
