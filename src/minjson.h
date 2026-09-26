// Minimal hand-written JSON value model (no third-party dependencies).
#pragma once

#include <cstddef>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace minjson {

enum class Type { Null, Boolean, Integer, Number, String, Array, Object };

class Value {
 public:
  Type type = Type::Null;
  bool boolean = false;
  long long integer = 0;
  double number = 0.0;
  std::string text;
  std::vector<Value> items;
  std::vector<std::pair<std::string, Value>> members;  // insertion ordered

  static Value makeNull() { return Value{}; }
  static Value makeBool(bool b) {
    Value v;
    v.type = Type::Boolean;
    v.boolean = b;
    return v;
  }
  static Value makeInt(long long i) {
    Value v;
    v.type = Type::Integer;
    v.integer = i;
    return v;
  }
  static Value makeString(std::string s) {
    Value v;
    v.type = Type::String;
    v.text = std::move(s);
    return v;
  }
  static Value makeArray() {
    Value v;
    v.type = Type::Array;
    return v;
  }
  static Value makeObject() {
    Value v;
    v.type = Type::Object;
    return v;
  }

  bool is(Type t) const { return type == t; }
  const Value* find(std::string_view key) const;
  Value* find(std::string_view key);
  bool has(std::string_view key) const { return find(key) != nullptr; }
  // Sets a member, replacing in place if the key exists (order preserved),
  // otherwise appending it.
  void set(const std::string& key, Value value);
  void push(Value value) { items.push_back(std::move(value)); }
};

struct ParseError {
  std::string message;
  std::size_t offset = 0;
};

// Parses a JSON document. Returns std::nullopt on failure; when `error` is
// non-null it receives a description and a byte offset.
std::optional<Value> parse(std::string_view input, ParseError* error = nullptr);

// Serializes a JSON document. `indent` <= 0 produces compact output.
std::string dump(const Value& value, int indent = 2);

}  // namespace minjson
