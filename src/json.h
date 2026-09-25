// json.h — small dependency-free JSON parser and writer (DOM).
//
// Supported subset, which covers the request schema:
//   objects, arrays, strings (with the common escapes), booleans, null,
//   and numbers parsed via strtod (so NaN/Infinity literals are NOT accepted;
//   the grammar requires a conventional JSON number).
// Malformed JSON produces an error message rather than throwing.
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace raybox {

class JsonValue {
 public:
  // Raw: verbatim JSON fragment produced by trusted server code (used for
  // fixed-precision numbers such as %.3f timing values).
  enum class Type { Null, Bool, Number, Integer, String, Array, Object, Raw };

  Type type = Type::Null;
  bool boolean = false;
  double number = 0.0;
  int64_t integer = 0;
  std::string str;  // string contents, or raw JSON fragment when Type::Raw
  std::vector<JsonValue> arr;
  std::map<std::string, JsonValue> obj;  // sorted keys; fine for our schema

  bool isObject() const { return type == Type::Object; }
  bool isArray() const { return type == Type::Array; }
  bool isNumber() const {
    return type == Type::Number || type == Type::Integer;
  }

  const JsonValue* find(const std::string& key) const {
    if (type != Type::Object) return nullptr;
    auto it = obj.find(key);
    return it == obj.end() ? nullptr : &it->second;
  }
};

// Parse `text`. On success returns true and sets `out`; otherwise false and
// fills `error` with a human-readable position.
bool jsonParse(const std::string& text, JsonValue& out, std::string& error);

// Serialize with enough digits for round-tripping doubles (17 significant).
std::string jsonDump(const JsonValue& v);

// Convenience builders.
JsonValue jsonObject();
JsonValue jsonArray();
// Wrap an already-valid JSON fragment (e.g. "33.718") emitted verbatim.
JsonValue jsonRaw(std::string fragment);

}  // namespace raybox
