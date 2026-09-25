// Minimal JSON value type, parser and serializer.
// Self-contained: no external dependencies. Supports objects, arrays,
// strings (with escapes and \uXXXX), numbers, booleans and null.
#pragma once

#include <map>
#include <string>
#include <vector>

namespace domjson {

struct JsonValue {
  enum class Type { Null, Bool, Number, String, Array, Object };

  Type type = Type::Null;
  bool boolean = false;
  double number = 0.0;
  std::string str;
  std::vector<JsonValue> arr;
  std::map<std::string, JsonValue> obj;

  static JsonValue make_null() { return JsonValue(); }
  static JsonValue make_bool(bool b) {
    JsonValue v;
    v.type = Type::Bool;
    v.boolean = b;
    return v;
  }
  static JsonValue make_number(double d) {
    JsonValue v;
    v.type = Type::Number;
    v.number = d;
    return v;
  }
  static JsonValue make_string(const std::string& s) {
    JsonValue v;
    v.type = Type::String;
    v.str = s;
    return v;
  }
  static JsonValue make_array() {
    JsonValue v;
    v.type = Type::Array;
    return v;
  }
  static JsonValue make_object() {
    JsonValue v;
    v.type = Type::Object;
    return v;
  }

  bool is_null() const { return type == Type::Null; }
  bool is_bool() const { return type == Type::Bool; }
  bool is_number() const { return type == Type::Number; }
  bool is_string() const { return type == Type::String; }
  bool is_array() const { return type == Type::Array; }
  bool is_object() const { return type == Type::Object; }

  // Returns nullptr when the key is absent.
  const JsonValue* find(const std::string& key) const {
    if (type != Type::Object) return nullptr;
    auto it = obj.find(key);
    return it == obj.end() ? nullptr : &it->second;
  }
};

// Parses `text` into `out`. Returns true on success; on failure returns
// false and fills `err` with a human-readable message including position.
bool parse_json(const std::string& text, JsonValue& out, std::string& err);

// Serializes `v` with 2-space indentation. Output is deterministic:
// object keys are emitted in sorted (std::map) order.
std::string dump_json(const JsonValue& v);

}  // namespace domjson
