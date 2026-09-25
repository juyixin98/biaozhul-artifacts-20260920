// json.hpp — 最小自包含 JSON 解析/序列化(仅支持本服务所需子集:
// 对象、数组、字符串、数字、true/false/null)。无外部依赖。
#pragma once

#include <cctype>
#include <cmath>
#include <iomanip>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace traj {
namespace json {

struct Value {
  enum class Type { Null, Bool, Number, String, Array, Object };
  Type type = Type::Null;
  bool boolean = false;
  double number = 0.0;
  std::string str;
  std::vector<Value> arr;
  std::map<std::string, Value> obj;

  static Value makeNumber(double v) { Value x; x.type = Type::Number; x.number = v; return x; }
  static Value makeString(std::string s) { Value x; x.type = Type::String; x.str = std::move(s); return x; }
  static Value makeBool(bool b) { Value x; x.type = Type::Bool; x.boolean = b; return x; }
  static Value makeArray() { Value x; x.type = Type::Array; return x; }
  static Value makeObject() { Value x; x.type = Type::Object; return x; }

  bool isNull() const { return type == Type::Null; }
  bool isNumber() const { return type == Type::Number; }
  bool isArray() const { return type == Type::Array; }
  bool isObject() const { return type == Type::Object; }
  const Value* find(const std::string& key) const {
    if (type != Type::Object) return nullptr;
    auto it = obj.find(key);
    return it == obj.end() ? nullptr : &it->second;
  }
};

class Parser {
 public:
  explicit Parser(const std::string& s) : s_(s) {}

  Value parse() {
    skipWs();
    Value v = parseValue();
    skipWs();
    if (pos_ != s_.size()) fail("trailing characters after JSON value");
    return v;
  }

 private:
  const std::string& s_;
  size_t pos_ = 0;

  [[noreturn]] void fail(const std::string& msg) const {
    throw std::invalid_argument("JSON parse error at offset " + std::to_string(pos_) + ": " + msg);
  }
  void skipWs() {
    while (pos_ < s_.size() && std::isspace(static_cast<unsigned char>(s_[pos_]))) ++pos_;
  }
  char peek() const { return pos_ < s_.size() ? s_[pos_] : '\0'; }
  char get() {
    if (pos_ >= s_.size()) fail("unexpected end of input");
    return s_[pos_++];
  }
  void expect(char c) {
    if (get() != c) fail(std::string("expected '") + c + "'");
  }
  void expectLiteral(const char* lit) {
    for (const char* p = lit; *p; ++p) {
      if (get() != *p) fail(std::string("invalid literal, expected '") + lit + "'");
    }
  }

  Value parseValue() {
    skipWs();
    switch (peek()) {
      case '{': return parseObject();
      case '[': return parseArray();
      case '"': { Value v; v.type = Value::Type::String; v.str = parseString(); return v; }
      case 't': expectLiteral("true"); return Value::makeBool(true);
      case 'f': expectLiteral("false"); return Value::makeBool(false);
      case 'n': expectLiteral("null"); return Value();
      default: return parseNumber();
    }
  }

  Value parseObject() {
    expect('{');
    Value v = Value::makeObject();
    skipWs();
    if (peek() == '}') { get(); return v; }
    while (true) {
      skipWs();
      if (peek() != '"') fail("expected string key in object");
      std::string key = parseString();
      skipWs();
      expect(':');
      v.obj[key] = parseValue();
      skipWs();
      const char c = get();
      if (c == '}') break;
      if (c != ',') fail("expected ',' or '}' in object");
    }
    return v;
  }

  Value parseArray() {
    expect('[');
    Value v = Value::makeArray();
    skipWs();
    if (peek() == ']') { get(); return v; }
    while (true) {
      v.arr.push_back(parseValue());
      skipWs();
      const char c = get();
      if (c == ']') break;
      if (c != ',') fail("expected ',' or ']' in array");
    }
    return v;
  }

  std::string parseString() {
    expect('"');
    std::string out;
    while (true) {
      const char c = get();
      if (c == '"') break;
      if (c == '\\') {
        const char e = get();
        switch (e) {
          case '"': out += '"'; break;
          case '\\': out += '\\'; break;
          case '/': out += '/'; break;
          case 'b': out += '\b'; break;
          case 'f': out += '\f'; break;
          case 'n': out += '\n'; break;
          case 'r': out += '\r'; break;
          case 't': out += '\t'; break;
          case 'u': {
            unsigned code = 0;
            for (int i = 0; i < 4; ++i) {
              const char h = get();
              code <<= 4;
              if (h >= '0' && h <= '9') code += h - '0';
              else if (h >= 'a' && h <= 'f') code += h - 'a' + 10;
              else if (h >= 'A' && h <= 'F') code += h - 'A' + 10;
              else fail("invalid \\u escape");
            }
            // 仅处理 BMP 基本字符的 UTF-8 编码,足够错误消息使用。
            if (code < 0x80) {
              out += static_cast<char>(code);
            } else if (code < 0x800) {
              out += static_cast<char>(0xC0 | (code >> 6));
              out += static_cast<char>(0x80 | (code & 0x3F));
            } else {
              out += static_cast<char>(0xE0 | (code >> 12));
              out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
              out += static_cast<char>(0x80 | (code & 0x3F));
            }
            break;
          }
          default: fail("invalid escape sequence");
        }
      } else {
        out += c;
      }
    }
    return out;
  }

  Value parseNumber() {
    const size_t start = pos_;
    if (peek() == '-') ++pos_;
    while (pos_ < s_.size() &&
           (std::isdigit(static_cast<unsigned char>(s_[pos_])) || s_[pos_] == '.' ||
            s_[pos_] == 'e' || s_[pos_] == 'E' || s_[pos_] == '+' || s_[pos_] == '-')) {
      ++pos_;
    }
    if (pos_ == start) fail("expected a value");
    try {
      return Value::makeNumber(std::stod(s_.substr(start, pos_ - start)));
    } catch (...) {
      fail("invalid number");
    }
  }
};

inline Value parse(const std::string& s) { return Parser(s).parse(); }

// 序列化:double 用 17 位有效数字,保证 double 往返无损。
inline void writeEscaped(std::ostringstream& os, const std::string& s) {
  os << '"';
  for (char c : s) {
    switch (c) {
      case '"': os << "\\\""; break;
      case '\\': os << "\\\\"; break;
      case '\n': os << "\\n"; break;
      case '\r': os << "\\r"; break;
      case '\t': os << "\\t"; break;
      default:
        if (static_cast<unsigned char>(c) < 0x20) {
          os << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<int>(c)
             << std::dec;
        } else {
          os << c;
        }
    }
  }
  os << '"';
}

inline void writeValue(std::ostringstream& os, const Value& v) {
  switch (v.type) {
    case Value::Type::Null: os << "null"; break;
    case Value::Type::Bool: os << (v.boolean ? "true" : "false"); break;
    case Value::Type::Number:
      if (std::isfinite(v.number)) {
        os << std::setprecision(17) << v.number;
      } else {
        os << "null";  // JSON 无法表示 inf/nan
      }
      break;
    case Value::Type::String: writeEscaped(os, v.str); break;
    case Value::Type::Array: {
      os << '[';
      for (size_t i = 0; i < v.arr.size(); ++i) {
        if (i) os << ',';
        writeValue(os, v.arr[i]);
      }
      os << ']';
      break;
    }
    case Value::Type::Object: {
      os << '{';
      bool first = true;
      for (const auto& [k, val] : v.obj) {
        if (!first) os << ',';
        first = false;
        writeEscaped(os, k);
        os << ':';
        writeValue(os, val);
      }
      os << '}';
      break;
    }
  }
}

inline std::string dump(const Value& v) {
  std::ostringstream os;
  writeValue(os, v);
  return os.str();
}

}  // namespace json
}  // namespace traj
