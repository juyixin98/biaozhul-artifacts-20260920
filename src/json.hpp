// Minimal JSON parser / emitter.
// Numbers preserve their original source token (JsonValue::numToken) so that
// decimal coordinates can later be scaled to integers exactly, without any
// floating-point parse.
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <vector>

struct JsonValue;
using JsonPtr = std::shared_ptr<JsonValue>;

enum class JsonType { Null, Bool, Number, String, Array, Object };

struct JsonValue {
    JsonType type = JsonType::Null;
    bool boolean = false;
    std::string numToken;                 // raw token when type == Number
    std::string str;                      // value when type == String
    std::vector<JsonPtr> arr;
    std::vector<std::pair<std::string, JsonPtr>> obj; // insertion ordered

    static JsonPtr makeNull();
    static JsonPtr makeBool(bool b);
    static JsonPtr makeNumber(std::string token);
    static JsonPtr makeString(std::string s);
    static JsonPtr makeArray();
    static JsonPtr makeObject();

    bool isNumber() const { return type == JsonType::Number; }
    bool isString() const { return type == JsonType::String; }

    const JsonPtr* get(const std::string& key) const;
    void set(const std::string& key, JsonPtr v);
};

struct JsonError {
    std::string message;
    size_t pos = 0;
};

// Parse JSON text. Throws std::runtime_error on malformed input.
JsonPtr jsonParse(const std::string& text);

// Serialize to compact JSON.
std::string jsonDump(const JsonValue& v);
std::string jsonDumpPretty(const JsonValue& v);
