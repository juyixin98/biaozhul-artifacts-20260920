#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace mcut {

// Minimal JSON value model. Hand-rolled recursive-descent parser so the whole
// backend has zero third-party dependencies. Only the subset needed by the
// protocol is supported: null, bool, integer/real number, string, array,
// object. Numbers that fit in int64 are stored as integers; larger or
// fractional numbers fall back to double.
class JsonValue {
 public:
  enum class Type { kNull, kBool, kInt, kDouble, kString, kArray, kObject };

  using Array = std::vector<JsonValue>;
  // std::map keeps object keys sorted, which makes output deterministic.
  using Object = std::map<std::string, JsonValue>;

  JsonValue() = default;

  Type type() const { return type_; }
  bool is(Type t) const { return type_ == t; }

  // Factories.
  static JsonValue null() { return JsonValue(); }
  static JsonValue boolean(bool b) { JsonValue v; v.type_ = Type::kBool; v.b_ = b; return v; }
  static JsonValue integer(std::int64_t i) { JsonValue v; v.type_ = Type::kInt; v.i_ = i; return v; }
  static JsonValue real(double d) { JsonValue v; v.type_ = Type::kDouble; v.d_ = d; return v; }
  static JsonValue string(std::string s) { JsonValue v; v.type_ = Type::kString; v.s_ = std::move(s); return v; }
  static JsonValue array(Array a = {}) { JsonValue v; v.type_ = Type::kArray; v.a_ = std::move(a); return v; }
  static JsonValue object(Object o = {}) { JsonValue v; v.type_ = Type::kObject; v.o_ = std::move(o); return v; }

  bool as_bool() const { return b_; }
  std::int64_t as_int() const { return type_ == Type::kInt ? i_ : static_cast<std::int64_t>(d_); }
  double as_double() const { return type_ == Type::kDouble ? d_ : static_cast<double>(i_); }
  const std::string& as_string() const { return s_; }
  const Array& as_array() const { return a_; }
  Array& as_array() { return a_; }
  const Object& as_object() const { return o_; }
  Object& as_object() { return o_; }

  // Object/array convenience accessors. `contains` works on any type but is
  // only meaningful for objects.
  bool contains(const std::string& key) const;
  // Returns nullptr when absent or wrong type.
  const JsonValue* find(const std::string& key) const;

 private:
  Type type_ = Type::kNull;
  bool b_ = false;
  std::int64_t i_ = 0;
  double d_ = 0.0;
  std::string s_;
  Array a_;
  Object o_;
};

struct JsonParseError {
  std::string message;
  size_t offset = 0;
};

// Parses a complete JSON document. Throws JsonParseError on malformed input.
JsonValue parse_json(const std::string& text);

// Serializes compactly (no insignificant whitespace).
std::string dump_json(const JsonValue& value);

}  // namespace mcut
