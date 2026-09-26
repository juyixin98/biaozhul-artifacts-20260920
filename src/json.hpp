// Minimal JSON value type, parser and serializer.
// Self-contained: no external dependencies.
#pragma once

#include <string>
#include <utility>
#include <vector>

struct JsonValue {
  enum class Type { Null, Bool, Number, String, Array, Object };

  Type type = Type::Null;
  bool boolean = false;
  double number = 0.0;
  std::string str;
  std::vector<JsonValue> arr;
  // Object members kept in insertion order so output is deterministic.
  std::vector<std::pair<std::string, JsonValue>> obj;

  static JsonValue makeNull();
  static JsonValue makeBool(bool b);
  static JsonValue makeNumber(double n);
  static JsonValue makeString(const std::string& s);
  static JsonValue makeArray();
  static JsonValue makeObject();

  bool isNull() const { return type == Type::Null; }
  bool isBool() const { return type == Type::Bool; }
  bool isNumber() const { return type == Type::Number; }
  bool isString() const { return type == Type::String; }
  bool isArray() const { return type == Type::Array; }
  bool isObject() const { return type == Type::Object; }

  // Returns nullptr when the key is absent or the value is not an object.
  const JsonValue* find(const std::string& key) const;

  // Human-readable one-line type name, for error messages.
  std::string typeName() const;

  std::string dump() const;
};

// Parses a complete JSON document. Throws std::runtime_error on invalid input.
JsonValue parseJson(const std::string& text);
