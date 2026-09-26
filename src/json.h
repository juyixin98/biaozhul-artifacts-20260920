// SPDX-License-Identifier: MIT
// Small strict JSON parser/serializer (RFC 8259 subset used by the API).
// Numbers are stored as their raw lexeme so that huge k values survive
// without floating-point loss.
#ifndef DAGPATHS_JSON_H
#define DAGPATHS_JSON_H

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace dagpaths {

enum class JsonType { Null, Bool, Number, String, Array, Object };

class JsonValue {
public:
    JsonType type = JsonType::Null;
    bool boolean = false;
    std::string number;        // raw lexeme, valid only for Number
    std::string str;           // unescaped text, valid only for String
    std::vector<JsonValue> arr;
    std::map<std::string, JsonValue> obj; // insertion-independent lookup

    static JsonValue makeNull() { return JsonValue{}; }
    static JsonValue makeBool(bool b) { JsonValue v; v.type = JsonType::Bool; v.boolean = b; return v; }
    static JsonValue makeNumber(std::string n) { JsonValue v; v.type = JsonType::Number; v.number = std::move(n); return v; }
    static JsonValue makeString(std::string s) { JsonValue v; v.type = JsonType::String; v.str = std::move(s); return v; }

    // Object accessors; return nullptr when absent or wrong-typed.
    const JsonValue* get(const std::string& key) const {
        if (type != JsonType::Object) return nullptr;
        auto it = obj.find(key);
        return it == obj.end() ? nullptr : &it->second;
    }
    bool isObject() const { return type == JsonType::Object; }
    bool isArray() const { return type == JsonType::Array; }
    bool isString() const { return type == JsonType::String; }
    bool isNumber() const { return type == JsonType::Number; }

    std::string dump() const;
};

// Parses a full JSON document. Throws std::invalid_argument with a
// position-aware message on any syntax error.
JsonValue parseJson(const std::string& text);

// Parses one JSON value beginning at pos (leading whitespace skipped),
// advancing pos to the first character after the value. Used by the
// CLI to consume a stream of concatenated/pretty-printed documents.
JsonValue parseJsonAt(const std::string& text, std::size_t& pos);

// Serialization helpers used when building responses.
std::string jsonEscape(const std::string& raw);
std::string dumpString(const std::string& raw);
std::string dumpInt(long long value);

} // namespace dagpaths

#endif
