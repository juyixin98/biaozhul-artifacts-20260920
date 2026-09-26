// Minimal, self-contained JSON parser/serializer.
// Supports: null, bool, integer, double, string (with \uXXXX / surrogate pairs),
// array, object (members keep insertion order). No third-party dependencies.
#pragma once

#include <cctype>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace json {

enum class Type { Null, Boolean, Integer, Real, String, Array, Object };

class Value {
 public:
  Type type = Type::Null;
  bool boolean = false;
  long long integer = 0;
  double real = 0.0;
  std::string text;
  std::vector<Value> items;
  std::vector<std::pair<std::string, Value>> members;

  Value() = default;

  static Value makeBoolean(bool value) {
    Value v;
    v.type = Type::Boolean;
    v.boolean = value;
    return v;
  }
  static Value makeInteger(long long value) {
    Value v;
    v.type = Type::Integer;
    v.integer = value;
    return v;
  }
  static Value makeReal(double value) {
    Value v;
    v.type = Type::Real;
    v.real = value;
    return v;
  }
  static Value makeString(std::string value) {
    Value v;
    v.type = Type::String;
    v.text = std::move(value);
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

  bool isNull() const { return type == Type::Null; }
  bool isBoolean() const { return type == Type::Boolean; }
  bool isInteger() const { return type == Type::Integer; }
  bool isReal() const { return type == Type::Real; }
  bool isString() const { return type == Type::String; }
  bool isArray() const { return type == Type::Array; }
  bool isObject() const { return type == Type::Object; }

  const Value* find(std::string_view key) const {
    for (const auto& member : members) {
      if (member.first == key) return &member.second;
    }
    return nullptr;
  }
};

class ParseError : public std::runtime_error {
 public:
  explicit ParseError(const std::string& message) : std::runtime_error(message) {}
};

Value parse(std::string_view input);
std::string dump(const Value& value, int indent = 2);

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

namespace detail {

inline void appendUtf8(std::string& out, uint32_t codepoint) {
  if (codepoint <= 0x7F) {
    out.push_back(static_cast<char>(codepoint));
  } else if (codepoint <= 0x7FF) {
    out.push_back(static_cast<char>(0xC0 | (codepoint >> 6)));
    out.push_back(static_cast<char>(0x80 | (codepoint & 0x3F)));
  } else if (codepoint <= 0xFFFF) {
    out.push_back(static_cast<char>(0xE0 | (codepoint >> 12)));
    out.push_back(static_cast<char>(0x80 | ((codepoint >> 6) & 0x3F)));
    out.push_back(static_cast<char>(0x80 | (codepoint & 0x3F)));
  } else {
    out.push_back(static_cast<char>(0xF0 | (codepoint >> 18)));
    out.push_back(static_cast<char>(0x80 | ((codepoint >> 12) & 0x3F)));
    out.push_back(static_cast<char>(0x80 | ((codepoint >> 6) & 0x3F)));
    out.push_back(static_cast<char>(0x80 | (codepoint & 0x3F)));
  }
}

class Parser {
 public:
  explicit Parser(std::string_view input) : s_(input) {}

  Value parse() {
    Value root = parseValue();
    skipWhitespace();
    if (p_ != s_.size()) {
      throw ParseError("trailing characters after JSON value at offset " +
                       std::to_string(p_));
    }
    return root;
  }

 private:
  std::string_view s_;
  size_t p_ = 0;

  [[noreturn]] void fail(const std::string& message) const {
    throw ParseError(message + " at offset " + std::to_string(p_));
  }

  bool eof() const { return p_ >= s_.size(); }

  char peek() const {
    if (eof()) throw ParseError("unexpected end of input");
    return s_[p_];
  }

  void skipWhitespace() {
    while (!eof()) {
      unsigned char c = static_cast<unsigned char>(s_[p_]);
      if (std::isspace(c)) {
        ++p_;
      } else {
        break;
      }
    }
  }

  Value parseValue() {
    skipWhitespace();
    if (eof()) fail("expected a value");
    char c = s_[p_];
    switch (c) {
      case '{': return parseObject();
      case '[': return parseArray();
      case '"': return Value::makeString(parseStringToken());
      case 't':
      case 'f': return parseBoolean();
      case 'n': return parseNull();
      default:
        if (c == '-' || std::isdigit(static_cast<unsigned char>(c))) {
          return parseNumber();
        }
        fail("unexpected character");
    }
  }

  Value parseObject() {
    Value object = Value::makeObject();
    ++p_;  // '{'
    skipWhitespace();
    if (!eof() && s_[p_] == '}') {
      ++p_;
      return object;
    }
    while (true) {
      skipWhitespace();
      if (eof() || s_[p_] != '"') fail("expected string key in object");
      std::string key = parseStringToken();
      skipWhitespace();
      if (eof() || s_[p_] != ':') fail("expected ':' after object key");
      ++p_;
      object.members.emplace_back(std::move(key), parseValue());
      skipWhitespace();
      if (eof()) fail("unterminated object");
      if (s_[p_] == ',') {
        ++p_;
        continue;
      }
      if (s_[p_] == '}') {
        ++p_;
        return object;
      }
      fail("expected ',' or '}' in object");
    }
  }

  Value parseArray() {
    Value array = Value::makeArray();
    ++p_;  // '['
    skipWhitespace();
    if (!eof() && s_[p_] == ']') {
      ++p_;
      return array;
    }
    while (true) {
      array.items.push_back(parseValue());
      skipWhitespace();
      if (eof()) fail("unterminated array");
      if (s_[p_] == ',') {
        ++p_;
        continue;
      }
      if (s_[p_] == ']') {
        ++p_;
        return array;
      }
      fail("expected ',' or ']' in array");
    }
  }

  std::string parseStringToken() {
    ++p_;  // opening quote
    std::string out;
    while (true) {
      if (eof()) fail("unterminated string");
      char c = s_[p_++];
      if (c == '"') return out;
      if (static_cast<unsigned char>(c) < 0x20) {
        fail("unescaped control character in string");
      }
      if (c != '\\') {
        out.push_back(c);  // raw UTF-8 bytes are passed through
        continue;
      }
      if (eof()) fail("unterminated escape sequence");
      char esc = s_[p_++];
      switch (esc) {
        case '"': out.push_back('"'); break;
        case '\\': out.push_back('\\'); break;
        case '/': out.push_back('/'); break;
        case 'b': out.push_back('\b'); break;
        case 'f': out.push_back('\f'); break;
        case 'n': out.push_back('\n'); break;
        case 'r': out.push_back('\r'); break;
        case 't': out.push_back('\t'); break;
        case 'u': {
          uint32_t high = parseHex4();
          uint32_t codepoint = high;
          if (high >= 0xD800 && high <= 0xDBFF) {
            if (p_ + 2 <= s_.size() && s_[p_] == '\\' && s_[p_ + 1] == 'u') {
              p_ += 2;
              uint32_t low = parseHex4();
              if (low >= 0xDC00 && low <= 0xDFFF) {
                codepoint = 0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00);
              } else {
                fail("invalid low surrogate");
              }
            } else {
              fail("expected low surrogate after high surrogate");
            }
          } else if (high >= 0xDC00 && high <= 0xDFFF) {
            fail("unexpected low surrogate");
          }
          appendUtf8(out, codepoint);
          break;
        }
        default:
          fail("invalid escape sequence");
      }
    }
  }

  uint32_t parseHex4() {
    if (p_ + 4 > s_.size()) fail("incomplete \\uXXXX escape");
    uint32_t value = 0;
    for (int i = 0; i < 4; ++i) {
      char c = s_[p_++];
      uint32_t digit;
      if (c >= '0' && c <= '9') digit = static_cast<uint32_t>(c - '0');
      else if (c >= 'a' && c <= 'f') digit = static_cast<uint32_t>(c - 'a' + 10);
      else if (c >= 'A' && c <= 'F') digit = static_cast<uint32_t>(c - 'A' + 10);
      else fail("invalid hexadecimal digit in \\uXXXX");
      value = (value << 4) | digit;
    }
    return value;
  }

  Value parseBoolean() {
    if (s_.compare(p_, 4, "true") == 0) {
      p_ += 4;
      return Value::makeBoolean(true);
    }
    if (s_.compare(p_, 5, "false") == 0) {
      p_ += 5;
      return Value::makeBoolean(false);
    }
    fail("invalid literal");
  }

  Value parseNull() {
    if (s_.compare(p_, 4, "null") == 0) {
      p_ += 4;
      return Value();
    }
    fail("invalid literal");
  }

  Value parseNumber() {
    size_t start = p_;
    bool isReal = false;
    if (!eof() && s_[p_] == '-') ++p_;
    if (eof()) fail("invalid number");
    if (s_[p_] == '0') {
      ++p_;
    } else {
      if (!std::isdigit(static_cast<unsigned char>(s_[p_]))) fail("invalid number");
      while (!eof() && std::isdigit(static_cast<unsigned char>(s_[p_]))) ++p_;
    }
    if (!eof() && s_[p_] == '.') {
      isReal = true;
      ++p_;
      if (eof() || !std::isdigit(static_cast<unsigned char>(s_[p_]))) {
        fail("invalid number: missing fraction digits");
      }
      while (!eof() && std::isdigit(static_cast<unsigned char>(s_[p_]))) ++p_;
    }
    if (!eof() && (s_[p_] == 'e' || s_[p_] == 'E')) {
      isReal = true;
      ++p_;
      if (!eof() && (s_[p_] == '+' || s_[p_] == '-')) ++p_;
      if (eof() || !std::isdigit(static_cast<unsigned char>(s_[p_]))) {
        fail("invalid number: missing exponent digits");
      }
      while (!eof() && std::isdigit(static_cast<unsigned char>(s_[p_]))) ++p_;
    }
    std::string token(s_.substr(start, p_ - start));
    if (isReal) {
      errno = 0;
      double value = std::strtod(token.c_str(), nullptr);
      if (errno == ERANGE) fail("number out of range");
      return Value::makeReal(value);
    }
    errno = 0;
    long long value = std::strtoll(token.c_str(), nullptr, 10);
    if (errno == ERANGE) fail("integer out of range");
    return Value::makeInteger(value);
  }
};

inline void writeEscapedString(std::string& out, const std::string& raw) {
  out.push_back('"');
  for (char ch : raw) {
    unsigned char c = static_cast<unsigned char>(ch);
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (c < 0x20) {
          char buffer[7];
          std::snprintf(buffer, sizeof(buffer), "\\u%04x", c);
          out += buffer;
        } else {
          out.push_back(static_cast<char>(c));  // UTF-8 passthrough
        }
    }
  }
  out.push_back('"');
}

inline void writeValue(std::string& out, const Value& value, int indent, int level) {
  auto writeNewlineIndent = [&](int lvl) {
    if (indent < 0) return;
    out.push_back('\n');
    out.append(static_cast<size_t>(indent * lvl), ' ');
  };
  switch (value.type) {
    case Type::Null: out += "null"; break;
    case Type::Boolean: out += value.boolean ? "true" : "false"; break;
    case Type::Integer: out += std::to_string(value.integer); break;
    case Type::Real: {
      char buffer[40];
      if (!std::isfinite(value.real)) {
        out += "null";
      } else {
        std::snprintf(buffer, sizeof(buffer), "%.17g", value.real);
        out += buffer;
      }
      break;
    }
    case Type::String: writeEscapedString(out, value.text); break;
    case Type::Array:
      if (value.items.empty()) {
        out += "[]";
        break;
      }
      out.push_back('[');
      for (size_t i = 0; i < value.items.size(); ++i) {
        if (i > 0) out.push_back(',');
        writeNewlineIndent(level + 1);
        writeValue(out, value.items[i], indent, level + 1);
      }
      writeNewlineIndent(level);
      out.push_back(']');
      break;
    case Type::Object:
      if (value.members.empty()) {
        out += "{}";
        break;
      }
      out.push_back('{');
      for (size_t i = 0; i < value.members.size(); ++i) {
        if (i > 0) out.push_back(',');
        writeNewlineIndent(level + 1);
        writeEscapedString(out, value.members[i].first);
        out += indent < 0 ? ":" : ": ";
        writeValue(out, value.members[i].second, indent, level + 1);
      }
      writeNewlineIndent(level);
      out.push_back('}');
      break;
  }
}

}  // namespace detail

inline Value parse(std::string_view input) {
  return detail::Parser(input).parse();
}

inline std::string dump(const Value& value, int indent) {
  std::string out;
  detail::writeValue(out, value, indent, 0);
  return out;
}

}  // namespace json
