#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace dcjson {

enum class JsonType { Null, Bool, Int, Double, String, Array, Object };

// A tagged-union style JSON value. Objects keep insertion order.
struct JsonValue {
  JsonType type = JsonType::Null;
  bool boolean = false;
  long long intVal = 0;
  double dblVal = 0.0;
  std::string str;
  std::vector<JsonValue> arr;
  std::vector<std::pair<std::string, JsonValue>> obj;

  static JsonValue makeNull() { return JsonValue{}; }
  static JsonValue makeBool(bool b) { JsonValue v; v.type = JsonType::Bool; v.boolean = b; return v; }
  static JsonValue makeInt(long long i) { JsonValue v; v.type = JsonType::Int; v.intVal = i; return v; }
  static JsonValue makeString(std::string s) { JsonValue v; v.type = JsonType::String; v.str = std::move(s); return v; }
  static JsonValue makeArray() { JsonValue v; v.type = JsonType::Array; return v; }
  static JsonValue makeObject() { JsonValue v; v.type = JsonType::Object; return v; }

  bool isInt() const { return type == JsonType::Int; }
  bool isString() const { return type == JsonType::String; }

  const JsonValue* find(const std::string& key) const;
  JsonValue* find(const std::string& key);
  void set(const std::string& key, JsonValue value);

  std::string dump(int indentStep = 2) const;
};

struct ParseResult {
  bool ok = false;
  std::string error;
  JsonValue value;
};

ParseResult parse(const std::string& text);

}  // namespace dcjson
