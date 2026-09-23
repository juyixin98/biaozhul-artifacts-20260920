#include "json.hpp"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <sstream>

namespace cps::json {

namespace {

struct Parser {
  const std::string& s;
  std::size_t pos = 0;
  std::string err;

  explicit Parser(const std::string& text) : s(text) {}

  void SkipWs() {
    while (pos < s.size()) {
      char c = s[pos];
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
        ++pos;
      } else {
        break;
      }
    }
  }

  bool Fail(const std::string& msg) {
    if (err.empty()) {
      std::ostringstream os;
      os << "JSON parse error at byte " << pos << ": " << msg;
      err = os.str();
    }
    return false;
  }

  bool ParseValue(Value& out) {
    SkipWs();
    if (pos >= s.size()) return Fail("unexpected end of input");
    char c = s[pos];
    switch (c) {
      case '{': return ParseObject(out);
      case '[': return ParseArray(out);
      case '"': {
        std::string str;
        if (!ParseString(str)) return false;
        out = Value(std::move(str));
        return true;
      }
      case 't': return ParseLit("true", Value(true), out);
      case 'f': return ParseLit("false", Value(false), out);
      case 'n': return ParseLit("null", Value(), out);
      default:
        if (c == '-' || (c >= '0' && c <= '9')) return ParseNumber(out);
        return Fail("unexpected character");
    }
  }

  bool ParseLit(const char* lit, Value val, Value& out) {
    const std::size_t len = std::char_traits<char>::length(lit);
    if (pos + len > s.size() || s.compare(pos, len, lit) != 0) {
      return Fail("invalid literal");
    }
    pos += len;
    out = std::move(val);
    return true;
  }

  bool ParseNumber(Value& out) {
    const std::size_t start = pos;
    if (s[pos] == '-') ++pos;
    if (pos >= s.size()) return Fail("bad number");
    if (s[pos] == '0') {
      ++pos;
    } else if (s[pos] >= '1' && s[pos] <= '9') {
      while (pos < s.size() && s[pos] >= '0' && s[pos] <= '9') ++pos;
    } else {
      return Fail("bad number");
    }
    if (pos < s.size() && s[pos] == '.') {
      ++pos;
      if (pos >= s.size() || s[pos] < '0' || s[pos] > '9')
        return Fail("bad number: expected digits after '.'");
      while (pos < s.size() && s[pos] >= '0' && s[pos] <= '9') ++pos;
    }
    if (pos < s.size() && (s[pos] == 'e' || s[pos] == 'E')) {
      ++pos;
      if (pos < s.size() && (s[pos] == '+' || s[pos] == '-')) ++pos;
      if (pos >= s.size() || s[pos] < '0' || s[pos] > '9')
        return Fail("bad number: malformed exponent");
      while (pos < s.size() && s[pos] >= '0' && s[pos] <= '9') ++pos;
    }
    const std::string token = s.substr(start, pos - start);
    try {
      out = Value(std::stod(token));
    } catch (...) {
      return Fail("number out of range");
    }
    return true;
  }

  bool ParseString(std::string& out) {
    // 假定 s[pos] == '"'
    ++pos;
    while (pos < s.size()) {
      char c = s[pos++];
      if (c == '"') return true;
      if (c == '\\') {
        if (pos >= s.size()) return Fail("unterminated escape");
        char e = s[pos++];
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
            if (pos + 4 > s.size()) return Fail("bad \\u escape");
            unsigned code = 0;
            for (int k = 0; k < 4; ++k) {
              char h = s[pos++];
              code <<= 4;
              if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
              else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
              else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
              else return Fail("bad \\u hex digit");
            }
            // 高代理项需要紧跟低代理项
            if (code >= 0xD800 && code <= 0xDBFF) {
              if (pos + 6 <= s.size() && s[pos] == '\\' && s[pos + 1] == 'u') {
                pos += 2;
                unsigned lo = 0;
                for (int k = 0; k < 4; ++k) {
                  char h = s[pos++];
                  lo <<= 4;
                  if (h >= '0' && h <= '9') lo |= static_cast<unsigned>(h - '0');
                  else if (h >= 'a' && h <= 'f') lo |= static_cast<unsigned>(h - 'a' + 10);
                  else if (h >= 'A' && h <= 'F') lo |= static_cast<unsigned>(h - 'A' + 10);
                  else return Fail("bad \\u hex digit");
                }
                if (lo >= 0xDC00 && lo <= 0xDFFF) {
                  code = 0x10000 + ((code - 0xD800) << 10) + (lo - 0xDC00);
                } else {
                  return Fail("expected low surrogate");
                }
              } else {
                return Fail("expected low surrogate after high surrogate");
              }
            } else if (code >= 0xDC00 && code <= 0xDFFF) {
              return Fail("unexpected low surrogate");
            }
            AppendUtf8(out, code);
            break;
          }
          default:
            return Fail("invalid escape character");
        }
      } else {
        if (static_cast<unsigned char>(c) < 0x20)
          return Fail("unescaped control character in string");
        out.push_back(c);
      }
    }
    return Fail("unterminated string");
  }

  static void AppendUtf8(std::string& out, unsigned code) {
    if (code < 0x80) {
      out.push_back(static_cast<char>(code));
    } else if (code < 0x800) {
      out.push_back(static_cast<char>(0xC0 | (code >> 6)));
      out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
    } else if (code < 0x10000) {
      out.push_back(static_cast<char>(0xE0 | (code >> 12)));
      out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
      out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
    } else {
      out.push_back(static_cast<char>(0xF0 | (code >> 18)));
      out.push_back(static_cast<char>(0x80 | ((code >> 12) & 0x3F)));
      out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
      out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
    }
  }

  bool ParseArray(Value& out) {
    out = Value::Array();
    ++pos;  // '['
    SkipWs();
    if (pos < s.size() && s[pos] == ']') {
      ++pos;
      return true;
    }
    while (true) {
      Value v;
      if (!ParseValue(v)) return false;
      out.push_back(std::move(v));
      SkipWs();
      if (pos >= s.size()) return Fail("unterminated array");
      if (s[pos] == ',') {
        ++pos;
        continue;
      }
      if (s[pos] == ']') {
        ++pos;
        return true;
      }
      return Fail("expected ',' or ']' in array");
    }
  }

  bool ParseObject(Value& out) {
    out = Value::Object();
    ++pos;  // '{'
    SkipWs();
    if (pos < s.size() && s[pos] == '}') {
      ++pos;
      return true;
    }
    while (true) {
      SkipWs();
      if (pos >= s.size() || s[pos] != '"')
        return Fail("expected string key in object");
      std::string key;
      if (!ParseString(key)) return false;
      SkipWs();
      if (pos >= s.size() || s[pos] != ':')
        return Fail("expected ':' after object key");
      ++pos;
      Value v;
      if (!ParseValue(v)) return false;
      out.set(std::move(key), std::move(v));
      SkipWs();
      if (pos >= s.size()) return Fail("unterminated object");
      if (s[pos] == ',') {
        ++pos;
        continue;
      }
      if (s[pos] == '}') {
        ++pos;
        return true;
      }
      return Fail("expected ',' or '}' in object");
    }
  }
};

void DumpString(std::string& os, const std::string& s) {
  os.push_back('"');
  for (unsigned char c : s) {
    switch (c) {
      case '"': os += "\\\""; break;
      case '\\': os += "\\\\"; break;
      case '\b': os += "\\b"; break;
      case '\f': os += "\\f"; break;
      case '\n': os += "\\n"; break;
      case '\r': os += "\\r"; break;
      case '\t': os += "\\t"; break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          os += buf;
        } else {
          os.push_back(static_cast<char>(c));
        }
    }
  }
  os.push_back('"');
}

void DumpValue(std::string& os, const Value& v) {
  switch (v.type()) {
    case Value::Type::Null:
      os += "null";
      break;
    case Value::Type::Bool:
      os += v.as_bool() ? "true" : "false";
      break;
    case Value::Type::Number: {
      double d = v.as_number();
      if (std::isnan(d) || std::isinf(d)) {
        os += "null";  // 标准 JSON 不能表示 NaN/Infinity
      } else {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%.17g", d);
        os += buf;
      }
      break;
    }
    case Value::Type::String:
      DumpString(os, v.as_string());
      break;
    case Value::Type::Array: {
      os.push_back('[');
      bool first = true;
      for (const Value& item : v.as_array()) {
        if (!first) os.push_back(',');
        first = false;
        DumpValue(os, item);
      }
      os.push_back(']');
      break;
    }
    case Value::Type::Object: {
      os.push_back('{');
      bool first = true;
      for (const auto& [k, item] : v.as_object()) {
        if (!first) os.push_back(',');
        first = false;
        DumpString(os, k);
        os.push_back(':');
        DumpValue(os, item);
      }
      os.push_back('}');
      break;
    }
  }
}

}  // namespace

Value Parse(const std::string& text, std::string* err) {
  Parser p(text);
  Value v;
  if (!p.ParseValue(v)) {
    if (err) *err = p.err;
    return Value();
  }
  p.SkipWs();
  if (p.pos != text.size()) {
    if (err) *err = "JSON parse error: trailing characters after value";
    return Value();
  }
  return v;
}

std::string Dump(const Value& v) {
  std::string os;
  DumpValue(os, v);
  return os;
}

}  // namespace cps::json
