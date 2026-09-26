#include "json.hpp"

#include <cctype>
#include <cmath>
#include <cstdio>
#include <stdexcept>

JsonValue JsonValue::makeNull() { return JsonValue{}; }

JsonValue JsonValue::makeBool(bool b) {
  JsonValue v;
  v.type = Type::Bool;
  v.boolean = b;
  return v;
}

JsonValue JsonValue::makeNumber(double n) {
  JsonValue v;
  v.type = Type::Number;
  v.number = n;
  return v;
}

JsonValue JsonValue::makeString(const std::string& s) {
  JsonValue v;
  v.type = Type::String;
  v.str = s;
  return v;
}

JsonValue JsonValue::makeArray() {
  JsonValue v;
  v.type = Type::Array;
  return v;
}

JsonValue JsonValue::makeObject() {
  JsonValue v;
  v.type = Type::Object;
  return v;
}

const JsonValue* JsonValue::find(const std::string& key) const {
  if (type != Type::Object) return nullptr;
  for (const auto& kv : obj) {
    if (kv.first == key) return &kv.second;
  }
  return nullptr;
}

std::string JsonValue::typeName() const {
  switch (type) {
    case Type::Null: return "null";
    case Type::Bool: return "boolean";
    case Type::Number: return "number";
    case Type::String: return "string";
    case Type::Array: return "array";
    case Type::Object: return "object";
  }
  return "unknown";
}

namespace {

void dumpEscaped(const std::string& s, std::string& out) {
  out += '"';
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
          out += c;
        }
    }
  }
  out += '"';
}

void dumpValue(const JsonValue& v, std::string& out) {
  switch (v.type) {
    case JsonValue::Type::Null:
      out += "null";
      break;
    case JsonValue::Type::Bool:
      out += v.boolean ? "true" : "false";
      break;
    case JsonValue::Type::Number: {
      double n = v.number;
      if (std::isfinite(n) && n == std::floor(n) && std::fabs(n) < 1e15) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "%lld", static_cast<long long>(n));
        out += buf;
      } else {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "%.17g", n);
        out += buf;
      }
      break;
    }
    case JsonValue::Type::String:
      dumpEscaped(v.str, out);
      break;
    case JsonValue::Type::Array:
      out += '[';
      for (size_t i = 0; i < v.arr.size(); ++i) {
        if (i) out += ',';
        dumpValue(v.arr[i], out);
      }
      out += ']';
      break;
    case JsonValue::Type::Object:
      out += '{';
      for (size_t i = 0; i < v.obj.size(); ++i) {
        if (i) out += ',';
        dumpEscaped(v.obj[i].first, out);
        out += ':';
        dumpValue(v.obj[i].second, out);
      }
      out += '}';
      break;
  }
}

class Parser {
 public:
  explicit Parser(const std::string& text) : text_(text) {}

  JsonValue parse() {
    skipWs();
    JsonValue v = parseValue();
    skipWs();
    if (pos_ != text_.size()) {
      fail("unexpected trailing characters");
    }
    return v;
  }

 private:
  const std::string& text_;
  size_t pos_ = 0;

  [[noreturn]] void fail(const std::string& msg) const {
    throw std::runtime_error("JSON parse error at offset " +
                             std::to_string(pos_) + ": " + msg);
  }

  void skipWs() {
    while (pos_ < text_.size() &&
           std::isspace(static_cast<unsigned char>(text_[pos_]))) {
      ++pos_;
    }
  }

  char peek() const {
    return pos_ < text_.size() ? text_[pos_] : '\0';
  }

  void expect(char c) {
    if (peek() != c) {
      fail(std::string("expected '") + c + "'");
    }
    ++pos_;
  }

  void expectLiteral(const char* lit) {
    for (const char* p = lit; *p; ++p) {
      if (peek() != *p) fail(std::string("expected literal '") + lit + "'");
      ++pos_;
    }
  }

  JsonValue parseValue() {
    skipWs();
    switch (peek()) {
      case '{': return parseObject();
      case '[': return parseArray();
      case '"': return JsonValue::makeString(parseString());
      case 't':
        expectLiteral("true");
        return JsonValue::makeBool(true);
      case 'f':
        expectLiteral("false");
        return JsonValue::makeBool(false);
      case 'n':
        expectLiteral("null");
        return JsonValue::makeNull();
      default:
        if (peek() == '-' || std::isdigit(static_cast<unsigned char>(peek()))) {
          return parseNumber();
        }
        fail("unexpected character");
    }
  }

  JsonValue parseObject() {
    JsonValue v = JsonValue::makeObject();
    expect('{');
    skipWs();
    if (peek() == '}') {
      ++pos_;
      return v;
    }
    while (true) {
      skipWs();
      if (peek() != '"') fail("expected object key string");
      std::string key = parseString();
      skipWs();
      expect(':');
      JsonValue val = parseValue();
      v.obj.emplace_back(std::move(key), std::move(val));
      skipWs();
      if (peek() == ',') {
        ++pos_;
        continue;
      }
      if (peek() == '}') {
        ++pos_;
        return v;
      }
      fail("expected ',' or '}' in object");
    }
  }

  JsonValue parseArray() {
    JsonValue v = JsonValue::makeArray();
    expect('[');
    skipWs();
    if (peek() == ']') {
      ++pos_;
      return v;
    }
    while (true) {
      v.arr.push_back(parseValue());
      skipWs();
      if (peek() == ',') {
        ++pos_;
        continue;
      }
      if (peek() == ']') {
        ++pos_;
        return v;
      }
      fail("expected ',' or ']' in array");
    }
  }

  void appendUtf8(unsigned long code, std::string& out) {
    if (code < 0x80) {
      out += static_cast<char>(code);
    } else if (code < 0x800) {
      out += static_cast<char>(0xC0 | (code >> 6));
      out += static_cast<char>(0x80 | (code & 0x3F));
    } else if (code < 0x10000) {
      out += static_cast<char>(0xE0 | (code >> 12));
      out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
      out += static_cast<char>(0x80 | (code & 0x3F));
    } else {
      out += static_cast<char>(0xF0 | (code >> 18));
      out += static_cast<char>(0x80 | ((code >> 12) & 0x3F));
      out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
      out += static_cast<char>(0x80 | (code & 0x3F));
    }
  }

  unsigned long parseHex4() {
    unsigned long code = 0;
    for (int i = 0; i < 4; ++i) {
      char c = peek();
      code <<= 4;
      if (c >= '0' && c <= '9') {
        code += c - '0';
      } else if (c >= 'a' && c <= 'f') {
        code += c - 'a' + 10;
      } else if (c >= 'A' && c <= 'F') {
        code += c - 'A' + 10;
      } else {
        fail("invalid \\u escape");
      }
      ++pos_;
    }
    return code;
  }

  std::string parseString() {
    expect('"');
    std::string out;
    while (true) {
      char c = peek();
      if (c == '\0') fail("unterminated string");
      if (c == '"') {
        ++pos_;
        return out;
      }
      if (c == '\\') {
        ++pos_;
        char esc = peek();
        ++pos_;
        switch (esc) {
          case '"': out += '"'; break;
          case '\\': out += '\\'; break;
          case '/': out += '/'; break;
          case 'b': out += '\b'; break;
          case 'f': out += '\f'; break;
          case 'n': out += '\n'; break;
          case 'r': out += '\r'; break;
          case 't': out += '\t'; break;
          case 'u': {
            unsigned long code = parseHex4();
            if (code >= 0xD800 && code <= 0xDBFF) {
              // Surrogate pair.
              if (peek() == '\\') {
                ++pos_;
                if (peek() == 'u') {
                  ++pos_;
                  unsigned long low = parseHex4();
                  if (low >= 0xDC00 && low <= 0xDFFF) {
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
                  } else {
                    fail("invalid low surrogate");
                  }
                } else {
                  fail("expected low surrogate");
                }
              } else {
                fail("expected low surrogate");
              }
            }
            appendUtf8(code, out);
            break;
          }
          default:
            fail("invalid escape sequence");
        }
      } else {
        out += c;
        ++pos_;
      }
    }
  }

  JsonValue parseNumber() {
    size_t start = pos_;
    if (peek() == '-') ++pos_;
    while (pos_ < text_.size() &&
           (std::isdigit(static_cast<unsigned char>(text_[pos_])) ||
            text_[pos_] == '.' || text_[pos_] == 'e' || text_[pos_] == 'E' ||
            text_[pos_] == '+' || text_[pos_] == '-')) {
      ++pos_;
    }
    try {
      return JsonValue::makeNumber(std::stod(text_.substr(start, pos_ - start)));
    } catch (...) {
      fail("invalid number");
    }
  }
};

}  // namespace

std::string JsonValue::dump() const {
  std::string out;
  dumpValue(*this, out);
  return out;
}

JsonValue parseJson(const std::string& text) {
  Parser p(text);
  return p.parse();
}
