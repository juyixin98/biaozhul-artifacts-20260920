// Minimal JSON value, parser and serializer. No third-party dependencies.
// Supports: null, bool, integer (int64), double, string, array, object.
// Object key order is preserved for stable, diff-friendly output.
#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace diffc {

struct Json {
  enum class Type { Null, Bool, Int, Double, Str, Arr, Obj };

  Type type = Type::Null;
  bool boolean = false;
  std::int64_t integer = 0;
  double number = 0.0;
  std::string str;
  std::vector<Json> arr;
  std::vector<std::pair<std::string, Json>> obj;

  static Json makeNull() { return Json{}; }
  static Json makeBool(bool v) {
    Json j;
    j.type = Type::Bool;
    j.boolean = v;
    return j;
  }
  static Json makeInt(std::int64_t v) {
    Json j;
    j.type = Type::Int;
    j.integer = v;
    return j;
  }
  static Json makeStr(const std::string& v) {
    Json j;
    j.type = Type::Str;
    j.str = v;
    return j;
  }
  static Json makeArr() {
    Json j;
    j.type = Type::Arr;
    return j;
  }
  static Json makeObj() {
    Json j;
    j.type = Type::Obj;
    return j;
  }

  // Object helpers. set() overwrites an existing key, else appends.
  void set(const std::string& key, Json value);
  const Json* find(const std::string& key) const;

  std::string dump(int indent = 2) const;
};

// Parses a complete JSON document. Throws JsonError on malformed input.
struct JsonError {
  std::string message;
};

Json parseJson(const std::string& text);

}  // namespace diffc
