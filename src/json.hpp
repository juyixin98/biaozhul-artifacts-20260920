#pragma once
// Minimal hand-rolled JSON parser/serializer. No third-party dependencies.
// Supports: null, bool, integer, double, string (with \uXXXX escapes),
// arrays, objects (std::map, deterministic key order).
#include <cstdint>
#include <map>
#include <stdexcept>
#include <string>
#include <variant>
#include <vector>

namespace json {

class Value {
public:
    enum Type { Null, Bool, Int, Double, String, Array, Object };
    using ArrayT = std::vector<Value>;
    using ObjectT = std::map<std::string, Value>;

    // Constructors are defined out-of-line in json.cpp: defining them in the
    // header lets GCC 13 at -O2 inline libstdc++ rb_tree code into every TU
    // that builds a JSON object, triggering known -Warray-bounds /
    // -Wmaybe-uninitialized false positives on std::variant<...,std::map>.
    Value();
    Value(std::nullptr_t);
    Value(bool b);
    Value(long long i);
    Value(int i);
    Value(long i);
    Value(double d);
    Value(const char* s);
    Value(std::string s);
    Value(ArrayT a);
    Value(ObjectT o);

    Type type() const { return static_cast<Type>(value_.index()); }
    bool isNull() const { return type() == Null; }
    bool isBool() const { return type() == Bool; }
    bool isInt() const { return type() == Int; }
    bool isNumber() const { return type() == Int || type() == Double; }
    bool isString() const { return type() == String; }
    bool isArray() const { return type() == Array; }
    bool isObject() const { return type() == Object; }

    bool asBool() const { return std::get<bool>(value_); }
    long long asInt() const { return std::get<long long>(value_); }
    double asDouble() const {
        if (type() == Int) return static_cast<double>(std::get<long long>(value_));
        return std::get<double>(value_);
    }
    const std::string& asString() const { return std::get<std::string>(value_); }
    const ArrayT& asArray() const { return std::get<ArrayT>(value_); }
    const ObjectT& asObject() const { return std::get<ObjectT>(value_); }

    ArrayT& mutableArray() { return std::get<ArrayT>(value_); }
    ObjectT& mutableObject() { return std::get<ObjectT>(value_); }

    bool contains(const std::string& key) const {
        return type() == Object && asObject().count(key) > 0;
    }
    const Value& operator[](const std::string& key) const {
        const ObjectT& o = asObject();
        auto it = o.find(key);
        if (it == o.end()) throw std::runtime_error("json: missing key: " + key);
        return it->second;
    }
    const Value& at(size_t i) const { return asArray().at(i); }
    size_t size() const {
        if (type() == Array) return asArray().size();
        if (type() == Object) return asObject().size();
        return 0;
    }

private:
    std::variant<std::nullptr_t, bool, long long, double, std::string,
                 ArrayT, ObjectT>
        value_;
};

Value parse(const std::string& text);
std::string dump(const Value& v, int indent = 2);

}  // namespace json
