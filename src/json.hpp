// Minimal dependency-free JSON parser / serializer for the spatial index.
//
// Parsing follows RFC 8259 with two explicit restrictions:
//   * only finite numbers are accepted (1e999 / -Infinity / NaN are rejected);
//   * document depth is limited to kMaxDepth.
// Number values remember their original lexical form ("raw token") so that
// coordinates echoed in responses are byte-identical to the request; numbers
// computed by the program are formatted with enough digits to round-trip.
#pragma once

#include <cctype>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace json {

class ParseError : public std::runtime_error {
 public:
  ParseError(const std::string& msg, std::size_t pos)
      : std::runtime_error(msg + " (offset " + std::to_string(pos) + ")"),
        offset_(pos) {}
  std::size_t offset() const noexcept { return offset_; }

 private:
  std::size_t offset_;
};

struct Value {
  enum class Type { Null, Bool, Number, String, Array, Object };

  Type type = Type::Null;
  bool boolean = false;
  long double number = 0.0L;
  std::string rawNumber;  // original token for numbers coming from the request
  std::string text;
  std::vector<Value> items;
  std::vector<std::pair<std::string, Value>> members;

  Value() = default;
  explicit Value(Type t) : type(t) {}

  static Value makeBool(bool b) {
    Value v(Type::Bool);
    v.boolean = b;
    return v;
  }
  static Value makeString(std::string s) {
    Value v(Type::String);
    v.text = std::move(s);
    return v;
  }
  static Value makeArray() { return Value(Type::Array); }
  static Value makeObject() { return Value(Type::Object); }
  // A number produced by the program (formatted with full precision on dump).
  static Value makeNumber(long double n) {
    Value v(Type::Number);
    v.number = n;
    return v;
  }
  // An integer produced by the program (emitted exactly as base-10 text).
  static Value makeInt(std::int64_t n) {
    Value v(Type::Number);
    v.number = static_cast<long double>(n);
    v.rawNumber = std::to_string(n);
    return v;
  }

  bool is(Type t) const noexcept { return type == t; }
  bool isObject() const noexcept { return type == Type::Object; }
  bool isArray() const noexcept { return type == Type::Array; }

  const Value* find(std::string_view key) const {
    if (type != Type::Object) return nullptr;
    for (const auto& kv : members) {
      if (kv.first == key) return &kv.second;
    }
    return nullptr;
  }
};

namespace detail {

inline void encodeUtf8(std::uint32_t cp, std::string& out) {
  if (cp <= 0x7F) {
    out.push_back(static_cast<char>(cp));
  } else if (cp <= 0x7FF) {
    out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
    out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
  } else if (cp <= 0xFFFF) {
    out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
    out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
    out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
  } else {
    out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
    out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
    out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
    out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
  }
}

class Parser {
 public:
  explicit Parser(const std::string& text) : s_(text) {}

  Value parse() {
    Value v = parseValue(0);
    skipWs();
    if (i_ != s_.size()) fail("trailing characters after JSON document");
    return v;
  }

 private:
  static constexpr int kMaxDepth = 64;

  [[noreturn]] void fail(const std::string& msg) const {
    throw ParseError(msg, i_);
  }

  void skipWs() {
    while (i_ < s_.size()) {
      char c = s_[i_];
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
        ++i_;
      } else {
        break;
      }
    }
  }

  char peekOrFail(const std::string& msg) {
    if (i_ >= s_.size()) fail(msg);
    return s_[i_];
  }

  Value parseValue(int depth) {
    if (depth > kMaxDepth) fail("JSON nesting too deep");
    skipWs();
    peekOrFail("unexpected end of JSON document");
    char c = s_[i_];
    switch (c) {
      case '{':
        return parseObject(depth);
      case '[':
        return parseArray(depth);
      case '"':
        return Value::makeString(parseString());
      case 't':
      case 'f':
        return parseLiteralBool();
      case 'n':
        return parseLiteralNull();
      default:
        if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
        fail("unexpected character");
    }
  }

  Value parseObject(int depth) {
    Value v(Value::Type::Object);
    ++i_;  // '{'
    skipWs();
    if (peekOrFail("unterminated object") == '}') {
      ++i_;
      return v;
    }
    while (true) {
      skipWs();
      if (peekOrFail("unterminated object") != '"') fail("expected string key");
      std::string key = parseString();
      skipWs();
      if (peekOrFail("unterminated object") != ':') fail("expected ':'");
      ++i_;
      v.members.emplace_back(std::move(key), parseValue(depth + 1));
      skipWs();
      char sep = peekOrFail("unterminated object");
      if (sep == ',') {
        ++i_;
        continue;
      }
      if (sep == '}') {
        ++i_;
        return v;
      }
      fail("expected ',' or '}'");
    }
  }

  Value parseArray(int depth) {
    Value v(Value::Type::Array);
    ++i_;  // '['
    skipWs();
    if (peekOrFail("unterminated array") == ']') {
      ++i_;
      return v;
    }
    while (true) {
      v.items.push_back(parseValue(depth + 1));
      skipWs();
      char sep = peekOrFail("unterminated array");
      if (sep == ',') {
        ++i_;
        continue;
      }
      if (sep == ']') {
        ++i_;
        return v;
      }
      fail("expected ',' or ']'");
    }
  }

  Value parseLiteralBool() {
    if (s_.compare(i_, 4, "true") == 0) {
      i_ += 4;
      return Value::makeBool(true);
    }
    if (s_.compare(i_, 5, "false") == 0) {
      i_ += 5;
      return Value::makeBool(false);
    }
    fail("invalid literal");
  }

  Value parseLiteralNull() {
    if (s_.compare(i_, 4, "null") == 0) {
      i_ += 4;
      return Value(Value::Type::Null);
    }
    fail("invalid literal");
  }

  // Scans the JSON number grammar, then converts with strtod (stored widened
  // to long double so the value is the exact binary64 token).
  Value parseNumber() {
    const std::size_t start = i_;
    if (s_[i_] == '-') ++i_;
    if (i_ >= s_.size()) fail("invalid number");
    if (s_[i_] == '0') {
      ++i_;
    } else if (s_[i_] >= '1' && s_[i_] <= '9') {
      while (i_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[i_]))) ++i_;
    } else {
      fail("invalid number");
    }
    if (i_ < s_.size() && s_[i_] == '.') {
      ++i_;
      if (i_ >= s_.size() || !std::isdigit(static_cast<unsigned char>(s_[i_])))
        fail("invalid fraction in number");
      while (i_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[i_]))) ++i_;
    }
    if (i_ < s_.size() && (s_[i_] == 'e' || s_[i_] == 'E')) {
      ++i_;
      if (i_ < s_.size() && (s_[i_] == '+' || s_[i_] == '-')) ++i_;
      if (i_ >= s_.size() || !std::isdigit(static_cast<unsigned char>(s_[i_])))
        fail("invalid exponent in number");
      while (i_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[i_]))) ++i_;
    }
    std::string token = s_.substr(start, i_ - start);
    const char* begin = token.c_str();
    char* end = nullptr;
    errno = 0;
    double parsed = std::strtod(begin, &end);
    if (end != begin + token.size() || errno == ERANGE || !std::isfinite(parsed))
      throw ParseError("number out of finite range", start);
    Value v(Value::Type::Number);
    v.number = static_cast<long double>(parsed);
    v.rawNumber = std::move(token);
    return v;
  }

  std::string parseString() {
    ++i_;  // opening quote
    std::string out;
    while (true) {
      if (i_ >= s_.size()) fail("unterminated string");
      char c = s_[i_++];
      if (c == '"') return out;
      if (static_cast<unsigned char>(c) < 0x20) fail("unescaped control character in string");
      if (c != '\\') {
        out.push_back(c);
        continue;
      }
      if (i_ >= s_.size()) fail("unterminated escape");
      char e = s_[i_++];
      switch (e) {
        case '"':
        case '\\':
        case '/':
          out.push_back(e);
          break;
        case 'b':
          out.push_back('\b');
          break;
        case 'f':
          out.push_back('\f');
          break;
        case 'n':
          out.push_back('\n');
          break;
        case 'r':
          out.push_back('\r');
          break;
        case 't':
          out.push_back('\t');
          break;
        case 'u': {
          std::uint32_t hi = parseHex4();
          std::uint32_t cp = hi;
          if (hi >= 0xD800 && hi <= 0xDBFF) {
            if (i_ + 1 < s_.size() && s_[i_] == '\\' && s_[i_ + 1] == 'u') {
              i_ += 2;
              std::uint32_t lo = parseHex4();
              if (lo < 0xDC00 || lo > 0xDFFF) fail("invalid low surrogate");
              cp = 0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00);
            } else {
              fail("expected low surrogate");
            }
          } else if (hi >= 0xDC00 && hi <= 0xDFFF) {
            fail("unexpected low surrogate");
          }
          encodeUtf8(cp, out);
          break;
        }
        default:
          fail("invalid escape");
      }
    }
  }

  std::uint32_t parseHex4() {
    if (i_ + 4 > s_.size()) fail("incomplete unicode escape");
    std::uint32_t cp = 0;
    for (int k = 0; k < 4; ++k) {
      char c = s_[i_++];
      cp <<= 4;
      if (c >= '0' && c <= '9') {
        cp |= static_cast<std::uint32_t>(c - '0');
      } else if (c >= 'a' && c <= 'f') {
        cp |= static_cast<std::uint32_t>(c - 'a' + 10);
      } else if (c >= 'A' && c <= 'F') {
        cp |= static_cast<std::uint32_t>(c - 'A' + 10);
      } else {
        fail("invalid hex digit in unicode escape");
      }
    }
    return cp;
  }

  const std::string& s_;
  std::size_t i_ = 0;
};

// Formats a program-generated number. Values that are exactly representable
// as binary64 are printed with 17 significant digits (round-trip safe for
// double); true extended-precision values use 21 digits (round-trip safe for
// 64-bit-significand 80-bit long double).
inline std::string formatNumber(long double x) {
  if (!std::isfinite(x)) throw std::runtime_error("refusing to emit non-finite number");
  std::ostringstream os;
  double asDouble = static_cast<double>(x);
  if (static_cast<long double>(asDouble) == x) {
    os << std::setprecision(17) << asDouble;
  } else {
    os << std::setprecision(21) << x;
  }
  return os.str();
}

inline void writeEscaped(const std::string& s, std::string& out) {
  out.push_back('"');
  for (char raw : s) {
    unsigned char c = static_cast<unsigned char>(raw);
    switch (c) {
      case '"':
        out += "\\\"";
        break;
      case '\\':
        out += "\\\\";
        break;
      case '\b':
        out += "\\b";
        break;
      case '\f':
        out += "\\f";
        break;
      case '\n':
        out += "\\n";
        break;
      case '\r':
        out += "\\r";
        break;
      case '\t':
        out += "\\t";
        break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out += buf;
        } else {
          out.push_back(static_cast<char>(c));
        }
    }
  }
  out.push_back('"');
}

inline void writeValue(const Value& v, std::string& out, int depth, int step) {
  switch (v.type) {
    case Value::Type::Null:
      out += "null";
      break;
    case Value::Type::Bool:
      out += v.boolean ? "true" : "false";
      break;
    case Value::Type::Number:
      out += v.rawNumber.empty() ? formatNumber(v.number) : v.rawNumber;
      break;
    case Value::Type::String:
      writeEscaped(v.text, out);
      break;
    case Value::Type::Array:
      if (v.items.empty()) {
        out += "[]";
        break;
      }
      out.push_back('[');
      for (std::size_t k = 0; k < v.items.size(); ++k) {
        if (k) out.push_back(',');
        out.push_back('\n');
        out.append(static_cast<std::size_t>((depth + 1) * step), ' ');
        writeValue(v.items[k], out, depth + 1, step);
      }
      out.push_back('\n');
      out.append(static_cast<std::size_t>(depth * step), ' ');
      out.push_back(']');
      break;
    case Value::Type::Object:
      if (v.members.empty()) {
        out += "{}";
        break;
      }
      out.push_back('{');
      for (std::size_t k = 0; k < v.members.size(); ++k) {
        if (k) out.push_back(',');
        out.push_back('\n');
        out.append(static_cast<std::size_t>((depth + 1) * step), ' ');
        writeEscaped(v.members[k].first, out);
        out += ": ";
        writeValue(v.members[k].second, out, depth + 1, step);
      }
      out.push_back('\n');
      out.append(static_cast<std::size_t>(depth * step), ' ');
      out.push_back('}');
      break;
  }
}

}  // namespace detail

inline Value parse(const std::string& text) { return detail::Parser(text).parse(); }

// Pretty-printed JSON (2-space indentation).
inline std::string dump(const Value& v) {
  std::string out;
  detail::writeValue(v, out, 0, 2);
  out.push_back('\n');
  return out;
}

}  // namespace json
