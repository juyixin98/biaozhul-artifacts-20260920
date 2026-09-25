#include "json.h"

#include <cmath>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <utility>

namespace raybox {

namespace {

struct Parser {
  const std::string& s;
  size_t pos = 0;
  std::string error;

  explicit Parser(const std::string& text) : s(text) {}

  [[noreturn]] void fail(const std::string& msg) {
    std::ostringstream oss;
    oss << "JSON error at byte " << pos << ": " << msg;
    error = oss.str();
    throw ParseError{};
  }
  struct ParseError {};

  void skipWs() {
    while (pos < s.size()) {
      char c = s[pos];
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r')
        ++pos;
      else
        break;
    }
  }

  bool consume(char c) {
    skipWs();
    if (pos < s.size() && s[pos] == c) {
      ++pos;
      return true;
    }
    return false;
  }

  void expect(char c, const char* what) {
    if (!consume(c)) fail(std::string("expected ") + what);
  }

  JsonValue parseValue() {
    skipWs();
    if (pos >= s.size()) fail("unexpected end of input");
    char c = s[pos];
    if (c == '{') return parseObject();
    if (c == '[') return parseArray();
    if (c == '"') {
      JsonValue v;
      v.type = JsonValue::Type::String;
      v.str = parseString();
      return v;
    }
    if (c == 't') return parseLiteral("true", true);
    if (c == 'f') return parseLiteral("false", false);
    if (c == 'n') return parseNull();
    if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
    fail("unexpected character");
  }

  JsonValue parseLiteral(const char* lit, bool value) {
    size_t len = std::strlen(lit);
    if (s.compare(pos, len, lit) != 0) fail("invalid literal");
    pos += len;
    JsonValue v;
    v.type = JsonValue::Type::Bool;
    v.boolean = value;
    return v;
  }

  JsonValue parseNull() {
    if (s.compare(pos, 4, "null") != 0) fail("invalid literal");
    pos += 4;
    return JsonValue{};
  }

  JsonValue parseNumber() {
    size_t start = pos;
    if (s[pos] == '-') ++pos;
    // Integer part: '0' alone, or 1-9 followed by digits. Rejecting leading
    // zeros keeps the parser strict; also scan the whole token ourselves so
    // strtoder cannot accept stray characters.
    auto digits = [&]() {
      size_t b = pos;
      while (pos < s.size() && s[pos] >= '0' && s[pos] <= '9') ++pos;
      return pos > b;
    };
    if (pos < s.size() && s[pos] == '0') {
      ++pos;
    } else {
      if (!digits()) fail("malformed number");
    }
    if (pos < s.size() && s[pos] == '.') {
      ++pos;
      if (!digits()) fail("malformed number: digits expected after '.'");
    }
    if (pos < s.size() && (s[pos] == 'e' || s[pos] == 'E')) {
      ++pos;
      if (pos < s.size() && (s[pos] == '+' || s[pos] == '-')) ++pos;
      if (!digits()) fail("malformed number: digits expected in exponent");
    }
    std::string token = s.substr(start, pos - start);
    char* end = nullptr;
    (void)end;  // token boundaries were already validated by our own scan
    JsonValue v;
    // Prefer an integer when the token has no fraction/exponent and fits:
    // box ids and counts then round-trip without a ".0" suffix.
    bool integralToken = token.find('.') == std::string::npos &&
                         token.find('e') == std::string::npos &&
                         token.find('E') == std::string::npos;
    if (integralToken) {
      errno = 0;
      long long iv = std::strtoll(token.c_str(), &end, 10);
      if (errno == 0) {
        v.type = JsonValue::Type::Integer;
        v.integer = static_cast<int64_t>(iv);
        return v;
      }
    }
    double d = std::strtod(token.c_str(), &end);
    v.type = JsonValue::Type::Number;
    v.number = d;
    return v;
  }

  std::string parseString() {
    expect('"', "string");
    std::string out;
    while (pos < s.size()) {
      char c = s[pos++];
      if (c == '"') return out;
      if (c == '\\') {
        if (pos >= s.size()) fail("bad escape");
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
            if (pos + 4 > s.size()) fail("bad \\u escape");
            unsigned code = 0;
            for (int i = 0; i < 4; ++i) {
              char h = s[pos++];
              code <<= 4;
              if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
              else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
              else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
              else fail("bad hex digit in \\u escape");
            }
            // Encode as UTF-8 (surrogate pairs are handled for completeness).
            auto putUtf8 = [&](unsigned cp) {
              if (cp < 0x80) {
                out.push_back(static_cast<char>(cp));
              } else if (cp < 0x800) {
                out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
                out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
              } else {
                out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
                out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
                out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
              }
            };
            if (code >= 0xD800 && code <= 0xDBFF) {
              if (pos + 6 <= s.size() && s[pos] == '\\' && s[pos + 1] == 'u') {
                pos += 2;
                unsigned lo = 0;
                for (int i = 0; i < 4; ++i) {
                  char h = s[pos++];
                  lo <<= 4;
                  if (h >= '0' && h <= '9') lo |= static_cast<unsigned>(h - '0');
                  else if (h >= 'a' && h <= 'f') lo |= static_cast<unsigned>(h - 'a' + 10);
                  else if (h >= 'A' && h <= 'F') lo |= static_cast<unsigned>(h - 'A' + 10);
                  else fail("bad hex digit in \\u escape");
                }
                if (lo >= 0xDC00 && lo <= 0xDFFF) {
                  unsigned cp = 0x10000 + ((code - 0xD800) << 10) + (lo - 0xDC00);
                  // Encode 4-byte UTF-8.
                  out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
                  out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
                  out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
                  out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
                } else {
                  fail("expected low surrogate");
                }
              } else {
                fail("expected low surrogate after high surrogate");
              }
            } else if (code >= 0xDC00 && code <= 0xDFFF) {
              fail("unexpected low surrogate");
            } else {
              putUtf8(code);
            }
            break;
          }
          default: fail("unknown escape");
        }
      } else {
        if (static_cast<unsigned char>(c) < 0x20) fail("control character in string");
        out.push_back(c);
      }
    }
    fail("unterminated string");
  }

  JsonValue parseArray() {
    expect('[', "'['");
    JsonValue v;
    v.type = JsonValue::Type::Array;
    skipWs();
    if (consume(']')) return v;
    while (true) {
      v.arr.push_back(parseValue());
      skipWs();
      if (consume(',')) continue;
      expect(']', "',' or ']'");
      return v;
    }
  }

  JsonValue parseObject() {
    expect('{', "'{'");
    JsonValue v;
    v.type = JsonValue::Type::Object;
    skipWs();
    if (consume('}')) return v;
    while (true) {
      skipWs();
      if (pos >= s.size() || s[pos] != '"') fail("expected object key");
      std::string key = parseString();
      expect(':', "':'");
      v.obj[key] = parseValue();
      skipWs();
      if (consume(',')) continue;
      expect('}', "',' or '}'");
      return v;
    }
  }
};

}  // namespace

bool jsonParse(const std::string& text, JsonValue& out, std::string& error) {
  Parser p(text);
  try {
    out = p.parseValue();
    p.skipWs();
    if (p.pos != text.size()) {
      error = "JSON error: trailing characters after value";
      return false;
    }
    return true;
  } catch (const Parser::ParseError&) {
    error = p.error;
    return false;
  }
}

namespace {

void dumpString(std::string& out, const std::string& s) {
  out.push_back('"');
  for (unsigned char c : s) {
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

void dumpImpl(std::string& out, const JsonValue& v) {
  char buf[40];
  switch (v.type) {
    case JsonValue::Type::Null: out += "null"; break;
    case JsonValue::Type::Bool: out += v.boolean ? "true" : "false"; break;
    case JsonValue::Type::Integer:
      std::snprintf(buf, sizeof(buf), "%lld",
                    static_cast<long long>(v.integer));
      out += buf;
      break;
    case JsonValue::Type::Number: {
      // 17 significant digits round-trip an IEEE double. Integers written as
      // doubles keep a ".0" suffix so the float/int distinction survives in
      // text and every numeric field reads back as the intended type.
      if (v.number == 0.0) {
        out += std::signbit(v.number) ? "-0.0" : "0.0";
      } else {
        std::snprintf(buf, sizeof(buf), "%.17g", v.number);
        out += buf;
        bool looksIntegral =
            out.find('.') == std::string::npos &&
            out.find('e') == std::string::npos &&
            out.find('E') == std::string::npos &&
            std::isfinite(v.number);
        if (looksIntegral) out += ".0";
      }
      break;
    }
    case JsonValue::Type::Raw:
      // Trusted, preformatted fragment — copy verbatim.
      out += v.str;
      break;
    case JsonValue::Type::String:
      dumpString(out, v.str);
      break;
    case JsonValue::Type::Array: {
      out.push_back('[');
      for (size_t i = 0; i < v.arr.size(); ++i) {
        if (i) out.push_back(',');
        dumpImpl(out, v.arr[i]);
      }
      out.push_back(']');
      break;
    }
    case JsonValue::Type::Object: {
      out.push_back('{');
      bool first = true;
      for (const auto& [k, val] : v.obj) {
        if (!first) out.push_back(',');
        first = false;
        dumpString(out, k);
        out.push_back(':');
        dumpImpl(out, val);
      }
      out.push_back('}');
      break;
    }
  }
}

}  // namespace

std::string jsonDump(const JsonValue& v) {
  std::string out;
  dumpImpl(out, v);
  return out;
}

JsonValue jsonObject() {
  JsonValue v;
  v.type = JsonValue::Type::Object;
  return v;
}

JsonValue jsonArray() {
  JsonValue v;
  v.type = JsonValue::Type::Array;
  return v;
}

JsonValue jsonRaw(std::string fragment) {
  JsonValue v;
  v.type = JsonValue::Type::Raw;
  v.str = std::move(fragment);
  return v;
}

}  // namespace raybox
