#include "json.hpp"

#include <cctype>
#include <cstdio>
#include <sstream>

namespace scc {
namespace {

struct Parser {
  const std::string& s;
  size_t pos = 0;
  int line = 1;
  int col = 1;

  explicit Parser(const std::string& text) : s(text) {}

  [[noreturn]] void fail(const std::string& msg) {
    throw JsonError("JSON parse error at line " + std::to_string(line) +
                    " col " + std::to_string(col) + ": " + msg);
  }

  bool eof() const { return pos >= s.size(); }

  char peek() const { return eof() ? '\0' : s[pos]; }

  char get() {
    if (eof()) fail("unexpected end of input");
    char c = s[pos++];
    if (c == '\n') {
      ++line;
      col = 1;
    } else {
      ++col;
    }
    return c;
  }

  void skipWs() {
    while (!eof()) {
      char c = peek();
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
        get();
      } else {
        break;
      }
    }
  }

  void expect(char c) {
    if (get() != c) fail(std::string("expected '") + c + "'");
  }

  Json parseValue() {
    skipWs();
    char c = peek();
    switch (c) {
      case '{': return parseObject();
      case '[': return parseArray();
      case '"': return Json(parseString());
      case 't': parseLiteral("true"); return Json(true);
      case 'f': parseLiteral("false"); return Json(false);
      case 'n': parseLiteral("null"); return Json(nullptr);
      default:
        if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
        fail(std::string("unexpected character '") + (c ? c : '?') + "'");
    }
  }

  void parseLiteral(const char* lit) {
    for (const char* p = lit; *p; ++p) {
      if (eof() || get() != *p) fail("invalid literal");
    }
  }

  Json parseObject() {
    expect('{');
    Json::Object obj;
    skipWs();
    if (peek() == '}') {
      get();
      return Json(std::move(obj));
    }
    while (true) {
      skipWs();
      if (peek() != '"') fail("expected object key string");
      std::string key = parseString();
      skipWs();
      expect(':');
      obj[key] = parseValue();
      skipWs();
      char c = get();
      if (c == '}') break;
      if (c != ',') fail("expected ',' or '}' in object");
    }
    return Json(std::move(obj));
  }

  Json parseArray() {
    expect('[');
    Json::Array arr;
    skipWs();
    if (peek() == ']') {
      get();
      return Json(std::move(arr));
    }
    while (true) {
      arr.push_back(parseValue());
      skipWs();
      char c = get();
      if (c == ']') break;
      if (c != ',') fail("expected ',' or ']' in array");
    }
    return Json(std::move(arr));
  }

  void appendUtf8(std::string& out, uint32_t cp) {
    if (cp < 0x80) {
      out.push_back(static_cast<char>(cp));
    } else if (cp < 0x800) {
      out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
      out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp < 0x10000) {
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

  uint32_t parseHex4() {
    uint32_t v = 0;
    for (int i = 0; i < 4; ++i) {
      char c = get();
      v <<= 4;
      if (c >= '0' && c <= '9') v |= static_cast<uint32_t>(c - '0');
      else if (c >= 'a' && c <= 'f') v |= static_cast<uint32_t>(c - 'a' + 10);
      else if (c >= 'A' && c <= 'F') v |= static_cast<uint32_t>(c - 'A' + 10);
      else fail("invalid \\u escape");
    }
    return v;
  }

  std::string parseString() {
    expect('"');
    std::string out;
    while (true) {
      if (eof()) fail("unterminated string");
      char c = get();
      if (c == '"') break;
      if (c == '\\') {
        char e = get();
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
            uint32_t cp = parseHex4();
            if (cp >= 0xD800 && cp <= 0xDBFF) {
              // High surrogate: expect a low surrogate next.
              if (get() != '\\' || get() != 'u') fail("expected low surrogate");
              uint32_t lo = parseHex4();
              if (lo < 0xDC00 || lo > 0xDFFF) fail("invalid low surrogate");
              cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
            }
            appendUtf8(out, cp);
            break;
          }
          default: fail("invalid escape sequence");
        }
      } else if (static_cast<unsigned char>(c) < 0x20) {
        fail("unescaped control character in string");
      } else {
        out.push_back(c);
      }
    }
    return out;
  }

  Json parseNumber() {
    size_t start = pos;
    if (peek() == '-') get();
    if (eof()) fail("invalid number");
    if (peek() == '0') {
      get();
    } else if (peek() >= '1' && peek() <= '9') {
      while (!eof() && std::isdigit(static_cast<unsigned char>(peek()))) get();
    } else {
      fail("invalid number");
    }
    bool isDouble = false;
    if (peek() == '.') {
      isDouble = true;
      get();
      if (eof() || !std::isdigit(static_cast<unsigned char>(peek())))
        fail("invalid number: missing fraction digits");
      while (!eof() && std::isdigit(static_cast<unsigned char>(peek()))) get();
    }
    if (peek() == 'e' || peek() == 'E') {
      isDouble = true;
      get();
      if (peek() == '+' || peek() == '-') get();
      if (eof() || !std::isdigit(static_cast<unsigned char>(peek())))
        fail("invalid number: missing exponent digits");
      while (!eof() && std::isdigit(static_cast<unsigned char>(peek()))) get();
    }
    std::string text = s.substr(start, pos - start);
    try {
      if (isDouble) return Json(std::stod(text));
      return Json(static_cast<int64_t>(std::stoll(text)));
    } catch (const std::out_of_range&) {
      // Fall back to double for integers that overflow int64.
      try {
        return Json(std::stod(text));
      } catch (...) {
        fail("number out of range");
      }
    } catch (const std::invalid_argument&) {
      fail("invalid number");
    }
  }
};

void dumpString(const std::string& s, std::string& out) {
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

void dumpValue(const Json& v, std::string& out, int indent, int depth) {
  switch (v.type()) {
    case Json::Type::kNull: out += "null"; return;
    case Json::Type::kBool: out += v.asBool() ? "true" : "false"; return;
    case Json::Type::kInt: out += std::to_string(v.asInt()); return;
    case Json::Type::kDouble: {
      char buf[32];
      std::snprintf(buf, sizeof(buf), "%.17g", v.asDouble());
      out += buf;
      return;
    }
    case Json::Type::kString: dumpString(v.asString(), out); return;
    case Json::Type::kArray: {
      const auto& arr = v.asArray();
      if (arr.empty()) {
        out += "[]";
        return;
      }
      out.push_back('[');
      for (size_t i = 0; i < arr.size(); ++i) {
        if (i) out.push_back(',');
        if (indent >= 0) {
          out.push_back('\n');
          out.append(static_cast<size_t>(indent) * (depth + 1), ' ');
        }
        dumpValue(arr[i], out, indent, depth + 1);
      }
      if (indent >= 0) {
        out.push_back('\n');
        out.append(static_cast<size_t>(indent) * depth, ' ');
      }
      out.push_back(']');
      return;
    }
    case Json::Type::kObject: {
      const auto& obj = v.asObject();
      if (obj.empty()) {
        out += "{}";
        return;
      }
      out.push_back('{');
      bool first = true;
      for (const auto& kv : obj) {
        if (!first) out.push_back(',');
        first = false;
        if (indent >= 0) {
          out.push_back('\n');
          out.append(static_cast<size_t>(indent) * (depth + 1), ' ');
        }
        dumpString(kv.first, out);
        out.push_back(':');
        if (indent >= 0) out.push_back(' ');
        dumpValue(kv.second, out, indent, depth + 1);
      }
      if (indent >= 0) {
        out.push_back('\n');
        out.append(static_cast<size_t>(indent) * depth, ' ');
      }
      out.push_back('}');
      return;
    }
  }
}

}  // namespace

Json Json::parse(const std::string& text) {
  Parser p(text);
  Json v = p.parseValue();
  p.skipWs();
  if (!p.eof()) p.fail("trailing characters after JSON value");
  return v;
}

std::string Json::dump(int indent) const {
  std::string out;
  dumpValue(*this, out, indent, 0);
  return out;
}

}  // namespace scc
