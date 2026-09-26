// Minimal JSON value/parser/serializer. No external dependencies.
// Supports: null, bool, integer, double, string, array, object.
#pragma once

#include <cctype>
#include <cstdint>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace minijson {

class Error : public std::runtime_error {
 public:
  explicit Error(const std::string& message) : std::runtime_error(message) {}
};

struct Value {
  enum class Type { kNull, kBool, kInt, kDouble, kString, kArray, kObject };

  Type type = Type::kNull;
  bool bool_value = false;
  long long int_value = 0;
  double double_value = 0.0;
  std::string string_value;
  std::vector<Value> array_value;
  std::map<std::string, Value> object_value;

  static Value null() { return Value(); }
  static Value boolean(bool b) {
    Value v;
    v.type = Type::kBool;
    v.bool_value = b;
    return v;
  }
  static Value integer(long long i) {
    Value v;
    v.type = Type::kInt;
    v.int_value = i;
    return v;
  }
  static Value number(double d) {
    Value v;
    v.type = Type::kDouble;
    v.double_value = d;
    return v;
  }
  static Value string(const std::string& s) {
    Value v;
    v.type = Type::kString;
    v.string_value = s;
    return v;
  }
  static Value array() {
    Value v;
    v.type = Type::kArray;
    return v;
  }
  static Value object() {
    Value v;
    v.type = Type::kObject;
    return v;
  }

  bool is_null() const { return type == Type::kNull; }
  bool has(const std::string& key) const {
    return type == Type::kObject && object_value.count(key) != 0;
  }
  const Value& at(const std::string& key) const {
    if (type != Type::kObject) {
      throw Error("expected object when reading key '" + key + "'");
    }
    auto it = object_value.find(key);
    if (it == object_value.end()) throw Error("missing key '" + key + "'");
    return it->second;
  }
  long long as_int() const {
    if (type != Type::kInt) throw Error("expected integer");
    return int_value;
  }
  bool as_bool() const {
    if (type != Type::kBool) throw Error("expected boolean");
    return bool_value;
  }
  const std::string& as_string() const {
    if (type != Type::kString) throw Error("expected string");
    return string_value;
  }
  const std::vector<Value>& as_array() const {
    if (type != Type::kArray) throw Error("expected array");
    return array_value;
  }
};

class Parser {
 public:
  explicit Parser(const std::string& text) : text_(text) {}

  Value parse() {
    skip_ws();
    Value v = parse_value();
    skip_ws();
    if (pos_ != text_.size()) throw Error("trailing characters after JSON value");
    return v;
  }

 private:
  const std::string& text_;
  size_t pos_ = 0;

  void skip_ws() {
    while (pos_ < text_.size() &&
           (text_[pos_] == ' ' || text_[pos_] == '\t' || text_[pos_] == '\n' ||
            text_[pos_] == '\r')) {
      ++pos_;
    }
  }

  char peek() const {
    if (pos_ >= text_.size()) throw Error("unexpected end of input");
    return text_[pos_];
  }

  char next() {
    char c = peek();
    ++pos_;
    return c;
  }

  void expect(char c) {
    if (next() != c) throw Error(std::string("expected '") + c + "'");
  }

  bool consume(char c) {
    if (pos_ < text_.size() && text_[pos_] == c) {
      ++pos_;
      return true;
    }
    return false;
  }

  Value parse_value() {
    char c = peek();
    if (c == '{') return parse_object();
    if (c == '[') return parse_array();
    if (c == '"') return Value::string(parse_string());
    if (c == 't') return parse_literal("true", Value::boolean(true));
    if (c == 'f') return parse_literal("false", Value::boolean(false));
    if (c == 'n') return parse_literal("null", Value::null());
    return parse_number();
  }

  Value parse_literal(const char* word, Value v) {
    for (const char* p = word; *p; ++p) {
      if (pos_ >= text_.size() || text_[pos_] != *p) {
        throw Error(std::string("invalid literal, expected '") + word + "'");
      }
      ++pos_;
    }
    return v;
  }

  Value parse_object() {
    expect('{');
    Value obj = Value::object();
    skip_ws();
    if (consume('}')) return obj;
    while (true) {
      skip_ws();
      std::string key = parse_string();
      skip_ws();
      expect(':');
      skip_ws();
      obj.object_value[key] = parse_value();
      skip_ws();
      if (consume('}')) return obj;
      expect(',');
    }
  }

  Value parse_array() {
    expect('[');
    Value arr = Value::array();
    skip_ws();
    if (consume(']')) return arr;
    while (true) {
      skip_ws();
      arr.array_value.push_back(parse_value());
      skip_ws();
      if (consume(']')) return arr;
      expect(',');
    }
  }

  static void append_utf8(std::string& out, unsigned code) {
    if (code < 0x80) {
      out.push_back(static_cast<char>(code));
    } else if (code < 0x800) {
      out.push_back(static_cast<char>(0xC0 | (code >> 6)));
      out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
    } else {
      out.push_back(static_cast<char>(0xE0 | (code >> 12)));
      out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
      out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
    }
  }

  std::string parse_string() {
    expect('"');
    std::string out;
    while (true) {
      char c = next();
      if (c == '"') return out;
      if (c == '\\') {
        char e = next();
        switch (e) {
          case '"': out.push_back('"'); break;
          case '\\': out.push_back('\\'); break;
          case '/': out.push_back('/'); break;
          case 'b': out.push_back('\b'); break;
          case 'f': out.push_back('\f'); break;
          case 'n': out.push_back('\n'); break;
          case 'r': out.push_back('\r'); break;
          case 't': out.push_back('\t'); break;
          case 'u': {
            unsigned code = 0;
            for (int i = 0; i < 4; ++i) {
              char h = next();
              code <<= 4;
              if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
              else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
              else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
              else throw Error("invalid \\u escape");
            }
            append_utf8(out, code);
            break;
          }
          default: throw Error("invalid escape sequence");
        }
      } else {
        out.push_back(c);
      }
    }
  }

  Value parse_number() {
    size_t start = pos_;
    consume('-');
    while (pos_ < text_.size() &&
           (std::isdigit(static_cast<unsigned char>(text_[pos_])) ||
            text_[pos_] == '.' || text_[pos_] == 'e' || text_[pos_] == 'E' ||
            text_[pos_] == '+' || text_[pos_] == '-')) {
      ++pos_;
    }
    if (pos_ == start) throw Error("invalid JSON value");
    std::string token = text_.substr(start, pos_ - start);
    bool is_double = token.find_first_of(".eE") != std::string::npos;
    try {
      if (is_double) return Value::number(std::stod(token));
      return Value::integer(std::stoll(token));
    } catch (const std::exception&) {
      throw Error("invalid number: " + token);
    }
  }
};

inline void serialize_string(const std::string& s, std::string& out) {
  out.push_back('"');
  for (char c : s) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (static_cast<unsigned char>(c) < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out += buf;
        } else {
          out.push_back(c);
        }
    }
  }
  out.push_back('"');
}

inline void serialize_impl(const Value& v, std::string& out, bool pretty, int indent) {
  const std::string pad = pretty ? std::string(static_cast<size_t>(indent) * 2, ' ') : "";
  const std::string pad2 =
      pretty ? std::string(static_cast<size_t>(indent + 1) * 2, ' ') : "";
  switch (v.type) {
    case Value::Type::kNull: out += "null"; break;
    case Value::Type::kBool: out += v.bool_value ? "true" : "false"; break;
    case Value::Type::kInt: out += std::to_string(v.int_value); break;
    case Value::Type::kDouble: {
      char buf[32];
      std::snprintf(buf, sizeof(buf), "%.17g", v.double_value);
      out += buf;
      break;
    }
    case Value::Type::kString: serialize_string(v.string_value, out); break;
    case Value::Type::kArray: {
      if (v.array_value.empty()) {
        out += "[]";
        break;
      }
      out.push_back('[');
      if (pretty) out.push_back('\n');
      for (size_t i = 0; i < v.array_value.size(); ++i) {
        if (i) {
          out.push_back(',');
          if (pretty) out.push_back('\n');
        }
        if (pretty) out += pad2;
        serialize_impl(v.array_value[i], out, pretty, indent + 1);
      }
      if (pretty) {
        out.push_back('\n');
        out += pad;
      }
      out.push_back(']');
      break;
    }
    case Value::Type::kObject: {
      if (v.object_value.empty()) {
        out += "{}";
        break;
      }
      out.push_back('{');
      if (pretty) out.push_back('\n');
      size_t i = 0;
      for (const auto& kv : v.object_value) {
        if (i++) {
          out.push_back(',');
          if (pretty) out.push_back('\n');
        }
        if (pretty) out += pad2;
        serialize_string(kv.first, out);
        out.push_back(':');
        if (pretty) out.push_back(' ');
        serialize_impl(kv.second, out, pretty, indent + 1);
      }
      if (pretty) {
        out.push_back('\n');
        out += pad;
      }
      out.push_back('}');
      break;
    }
  }
}

inline std::string serialize(const Value& v) {
  std::string out;
  serialize_impl(v, out, false, 0);
  return out;
}

inline std::string serialize_pretty(const Value& v) {
  std::string out;
  serialize_impl(v, out, true, 0);
  return out;
}

}  // namespace minijson
