// Minimal RFC 8259 JSON parser + writer (no external dependencies).
// Supports objects, arrays, strings, numbers (double), booleans, null.
#ifndef PGO_JSON_HPP
#define PGO_JSON_HPP

#include <cstdint>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace pgo {

class JsonError : public std::runtime_error {
 public:
  JsonError(const std::string& msg, int line = 0, int col = 0);
  int line() const { return line_; }
  int col() const { return col_; }

 private:
  int line_;
  int col_;
};

class JsonValue {
 public:
  enum class Type { Null, Bool, Number, String, Array, Object };

  JsonValue() : type_(Type::Null) {}
  static JsonValue makeBool(bool b) { JsonValue v; v.type_ = Type::Bool; v.bool_ = b; return v; }
  static JsonValue makeNumber(double d) { JsonValue v; v.type_ = Type::Number; v.num_ = d; return v; }
  static JsonValue makeString(std::string s) { JsonValue v; v.type_ = Type::String; v.str_ = std::move(s); return v; }
  static JsonValue makeArray() { JsonValue v; v.type_ = Type::Array; return v; }
  static JsonValue makeObject() { JsonValue v; v.type_ = Type::Object; return v; }

  Type type() const { return type_; }
  bool isNull() const { return type_ == Type::Null; }
  bool isObject() const { return type_ == Type::Object; }
  bool isArray() const { return type_ == Type::Array; }
  bool isNumber() const { return type_ == Type::Number; }
  bool isString() const { return type_ == Type::String; }

  double asNumber() const;
  const std::string& asString() const;
  bool asBool() const;
  const std::vector<JsonValue>& asArray() const;
  // Ordered object: deterministic output, and the input files are small.
  const std::map<std::string, JsonValue>& asObject() const;

  bool contains(const std::string& key) const;
  const JsonValue& at(const std::string& key) const;
  // Returns null value if key missing.
  const JsonValue& find(const std::string& key) const;

  void set(const std::string& key, JsonValue v);
  void push(JsonValue v);

  // Pretty-printed JSON; indent=0 yields compact output.
  std::string dump(int indent = 2) const;

 private:
  Type type_;
  bool bool_ = false;
  double num_ = 0.0;
  std::string str_;
  std::vector<JsonValue> arr_;
  std::map<std::string, JsonValue> obj_;

  void dumpTo(std::string& out, int indent, int depth) const;
};

// Parse a complete JSON document. Throws JsonError with line/column on failure.
JsonValue parseJson(const std::string& text);

// Convenience: read + parse a file.
JsonValue loadJsonFile(const std::string& path);
void saveJsonFile(const std::string& path, const JsonValue& value, int indent = 2);

}  // namespace pgo

#endif
