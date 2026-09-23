// 最小可用的 JSON 解析器与序列化器（无第三方依赖）。
// 仅支持标准 JSON；对象保持插入顺序。数字统一以 double 承载，
// 序列化时整数形式的值不带小数点，其余使用 17 位有效数字（round-trip）。
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace js {

enum class JType { Null, Bool, Number, Integer, String, Array, Object };

class JValue {
public:
    using Array = std::vector<JValue>;
    using Object = std::vector<std::pair<std::string, JValue>>;  // 保持插入顺序

    JValue() : type_(JType::Null) {}
    static JValue makeBool(bool b) { JValue v; v.type_ = JType::Bool; v.b_ = b; return v; }
    static JValue makeNumber(double d) { JValue v; v.type_ = JType::Number; v.num_ = d; return v; }
    static JValue makeInt(int64_t i) { JValue v; v.type_ = JType::Integer; v.int_ = i; return v; }
    static JValue makeString(std::string s) { JValue v; v.type_ = JType::String; v.str_ = std::move(s); return v; }
    static JValue makeArray() { JValue v; v.type_ = JType::Array; v.arr_ = std::make_shared<Array>(); return v; }
    static JValue makeObject() { JValue v; v.type_ = JType::Object; v.obj_ = std::make_shared<Object>(); return v; }

    JType type() const { return type_; }
    bool isNull() const { return type_ == JType::Null; }
    bool isBool() const { return type_ == JType::Bool; }
    bool isNumber() const { return type_ == JType::Number; }
    bool isInt() const { return type_ == JType::Integer; }
    bool isString() const { return type_ == JType::String; }
    bool isArray() const { return type_ == JType::Array; }
    bool isObject() const { return type_ == JType::Object; }

    bool asBool() const { return b_; }
    double asNumber() const { return type_ == JType::Integer ? static_cast<double>(int_) : num_; }
    int64_t asInt() const { return int_; }
    const std::string& asString() const { return str_; }
    Array& asArray() { return *arr_; }
    const Array& asArray() const { return *arr_; }
    Object& asObject() { return *obj_; }
    const Object& asObject() const { return *obj_; }

    // 对象按键查找；不存在返回 nullptr。
    const JValue* find(const std::string& key) const {
        if (!isObject()) return nullptr;
        for (const auto& kv : *obj_) {
            if (kv.first == key) return &kv.second;
        }
        return nullptr;
    }

    void push(JValue v) { arr_->push_back(std::move(v)); }
    void set(std::string key, JValue v) {
        for (auto& kv : *obj_) {
            if (kv.first == key) { kv.second = std::move(v); return; }
        }
        obj_->emplace_back(std::move(key), std::move(v));
    }

    // 紧凑序列化（无多余空白）。
    std::string dump() const;

private:
    void dumpInto(std::string& out) const;

    JType type_;
    bool b_ = false;
    double num_ = 0.0;
    int64_t int_ = 0;
    std::string str_;
    std::shared_ptr<Array> arr_;
    std::shared_ptr<Object> obj_;
};

// 解析；失败时 ok=false 且 err 含位置信息。
JValue parse(const std::string& text, bool& ok, std::string& err);

}  // namespace js
