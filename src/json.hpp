// Minimal, dependency-free JSON parser/serializer.
// Numbers are kept as their original source text ("raw") so the geometry
// layer can parse coordinates exactly (no double round-trip).
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
    std::string raw;  // original number text (Number)
    std::string str;  // unescaped string value (String)
    std::vector<Value> arr;
    std::vector<std::pair<std::string, Value>> obj; // insertion ordered

    const Value* find(const std::string& key) const;
};

struct ParseResult {
    bool ok = false;
    std::string error;
    Value root;
};

ParseResult parse(const std::string& text);

// Builders for responses.
Value makeNull();
Value makeBool(bool b);
Value makeRawNumber(std::string raw);
Value makeInt(int64_t v);
Value makeString(std::string s);
Value makeArray();
Value makeObject();
void push(Value& arr, Value v);
void set(Value& obj, std::string key, Value v);

std::string dump(const Value& v); // compact
std::string dumpPretty(const Value& v);

} // namespace json
