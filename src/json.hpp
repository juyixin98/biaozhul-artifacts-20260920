// Minimal JSON parser / writer (no external dependencies).
// Supports: null, bool, integer/real number, string, array, object.
#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace json {

class Error : public std::runtime_error {
public:
    explicit Error(const std::string& msg) : std::runtime_error(msg) {}
};

class Value {
public:
    enum Type { Null, Bool, Number, String, Array, Object };

    Type type = Null;
    bool boolean = false;
    double number = 0.0;
    std::string str;
    std::vector<Value> arr;
    std::vector<std::pair<std::string, Value>> obj; // insertion-ordered

    static Value parse(const std::string& text);

    // Typed accessors; throw json::Error on type/shape mismatch.
    bool isObject() const { return type == Object; }
    bool isArray() const { return type == Array; }
    bool has(const std::string& key) const;
    const Value& at(const std::string& key) const;
    std::int64_t asInt() const; // fractional / out-of-range throws

    // Serializers.
    std::string dump() const;             // compact
    std::string dumpPretty(int indent = 2) const;

    // Builders used to assemble the response document.
    static Value makeObject() { Value v; v.type = Object; return v; }
    static Value makeArray() { Value v; v.type = Array; return v; }
    static Value fromInt(std::int64_t x) { Value v; v.type = Number; v.number = static_cast<double>(x); return v; }
    static Value fromBool(bool b) { Value v; v.type = Bool; v.boolean = b; return v; }
    static Value fromString(std::string s) { Value v; v.type = String; v.str = std::move(s); return v; }

    void set(const std::string& key, Value value);
    void push(Value value);

private:
    void dumpTo(std::string& out, int depth, int indent) const;
};

} // namespace json
