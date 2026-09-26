#include "json.hpp"

#include <cctype>
#include <stdexcept>

namespace diffc {

void Json::set(const std::string& key, Json value) {
  if (type != Type::Obj) {
    type = Type::Obj;
    obj.clear();
  }
  for (auto& kv : obj) {
    if (kv.first == key) {
      kv.second = std::move(value);
      return;
    }
  }
  obj.emplace_back(key, std::move(value));
}

const Json* Json::find(const std::string& key) const {
  if (type != Type::Obj) return nullptr;
  for (const auto& kv : obj) {
    if (kv.first == key) return &kv.second;
  }
  return nullptr;
}

namespace {

void dumpEscaped(const std::string& s, std::string& out) {
  out.push_back('"');
  for (char c : s) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\t': out += "\\t"; break;
      case '\r': out += "\\r"; break;
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

void dumpImpl(const Json& j, std::string& out, int indent, int depth) {
  const std::string pad(static_cast<size_t>(indent) * (depth + 1), ' ');
  const std::string padEnd(static_cast<size_t>(indent) * depth, ' ');
  switch (j.type) {
    case Json::Type::Null: out += "null"; break;
    case Json::Type::Bool: out += j.boolean ? "true" : "false"; break;
    case Json::Type::Int: out += std::to_string(j.integer); break;
    case Json::Type::Double: out += std::to_string(j.number); break;
    case Json::Type::Str: dumpEscaped(j.str, out); break;
    case Json::Type::Arr: {
      if (j.arr.empty()) {
        out += "[]";
        break;
      }
      out += "[\n";
      for (size_t i = 0; i < j.arr.size(); ++i) {
        out += pad;
        dumpImpl(j.arr[i], out, indent, depth + 1);
        if (i + 1 < j.arr.size()) out += ",";
        out += "\n";
      }
      out += padEnd + "]";
      break;
    }
    case Json::Type::Obj: {
      if (j.obj.empty()) {
        out += "{}";
        break;
      }
      out += "{\n";
      for (size_t i = 0; i < j.obj.size(); ++i) {
        out += pad;
        dumpEscaped(j.obj[i].first, out);
        out += ": ";
        dumpImpl(j.obj[i].second, out, indent, depth + 1);
        if (i + 1 < j.obj.size()) out += ",";
        out += "\n";
      }
      out += padEnd + "}";
      break;
    }
  }
}

struct Parser {
  const std::string& s;
  size_t pos = 0;

  explicit Parser(const std::string& text) : s(text) {}

  [[noreturn]] void fail(const std::string& msg) {
    throw JsonError{msg + " (at byte " + std::to_string(pos) + ")"};
  }

  void skipWs() {
    while (pos < s.size() &&
           (s[pos] == ' ' || s[pos] == '\t' || s[pos] == '\n' || s[pos] == '\r')) {
      ++pos;
    }
  }

  char peek() {
    if (pos >= s.size()) fail("unexpected end of input");
    return s[pos];
  }

  void expect(char c) {
    if (peek() != c) fail(std::string("expected '") + c + "'");
    ++pos;
  }

  void expectLiteral(const char* lit) {
    for (const char* p = lit; *p; ++p) {
      if (pos >= s.size() || s[pos] != *p) fail("invalid literal");
      ++pos;
    }
  }

  Json parseValue() {
    skipWs();
    char c = peek();
    if (c == '{') return parseObject();
    if (c == '[') return parseArray();
    if (c == '"') {
      Json j = Json::makeStr("");
      j.str = parseString();
      return j;
    }
    if (c == 't') {
      expectLiteral("true");
      return Json::makeBool(true);
    }
    if (c == 'f') {
      expectLiteral("false");
      return Json::makeBool(false);
    }
    if (c == 'n') {
      expectLiteral("null");
      return Json::makeNull();
    }
    if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
    fail("unexpected character");
  }

  Json parseObject() {
    Json j = Json::makeObj();
    expect('{');
    skipWs();
    if (peek() == '}') {
      ++pos;
      return j;
    }
    while (true) {
      skipWs();
      std::string key = parseString();
      skipWs();
      expect(':');
      j.set(key, parseValue());
      skipWs();
      char c = peek();
      ++pos;
      if (c == '}') return j;
      if (c != ',') fail("expected ',' or '}' in object");
    }
  }

  Json parseArray() {
    Json j = Json::makeArr();
    expect('[');
    skipWs();
    if (peek() == ']') {
      ++pos;
      return j;
    }
    while (true) {
      j.arr.push_back(parseValue());
      skipWs();
      char c = peek();
      ++pos;
      if (c == ']') return j;
      if (c != ',') fail("expected ',' or ']' in array");
    }
  }

  std::string parseString() {
    expect('"');
    std::string out;
    while (true) {
      if (pos >= s.size()) fail("unterminated string");
      char c = s[pos++];
      if (c == '"') return out;
      if (c == '\\') {
        if (pos >= s.size()) fail("unterminated escape");
        char e = s[pos++];
        switch (e) {
          case '"': out.push_back('"'); break;
          case '\\': out.push_back('\\'); break;
          case '/': out.push_back('/'); break;
          case 'n': out.push_back('\n'); break;
          case 't': out.push_back('\t'); break;
          case 'r': out.push_back('\r'); break;
          case 'b': out.push_back('\b'); break;
          case 'f': out.push_back('\f'); break;
          case 'u': {
            if (pos + 4 > s.size()) fail("bad \\u escape");
            unsigned code = 0;
            for (int k = 0; k < 4; ++k) {
              char h = s[pos++];
              code <<= 4;
              if (h >= '0' && h <= '9') code += h - '0';
              else if (h >= 'a' && h <= 'f') code += h - 'a' + 10;
              else if (h >= 'A' && h <= 'F') code += h - 'A' + 10;
              else fail("bad hex digit in \\u escape");
            }
            // Minimal UTF-8 encoding (BMP only, no surrogate pairs).
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
            break;
          }
          default: fail("unknown escape");
        }
      } else {
        out.push_back(c);
      }
    }
  }

  Json parseNumber() {
    size_t start = pos;
    if (peek() == '-') ++pos;
    bool isDouble = false;
    while (pos < s.size() &&
           (std::isdigit(static_cast<unsigned char>(s[pos])) || s[pos] == '.' ||
            s[pos] == 'e' || s[pos] == 'E' || s[pos] == '+' || s[pos] == '-')) {
      if (s[pos] == '.' || s[pos] == 'e' || s[pos] == 'E') isDouble = true;
      ++pos;
    }
    if (pos == start || (pos == start + 1 && s[start] == '-')) fail("bad number");
    std::string token = s.substr(start, pos - start);
    try {
      if (isDouble) {
        Json j;
        j.type = Json::Type::Double;
        j.number = std::stod(token);
        return j;
      }
      return Json::makeInt(std::stoll(token));
    } catch (const std::out_of_range&) {
      fail("number out of range");
    }
  }
};

}  // namespace

std::string Json::dump(int indent) const {
  std::string out;
  dumpImpl(*this, out, indent, 0);
  out += "\n";
  return out;
}

Json parseJson(const std::string& text) {
  Parser p(text);
  Json v = p.parseValue();
  p.skipWs();
  if (p.pos != text.size()) p.fail("trailing characters after JSON document");
  return v;
}

}  // namespace diffc
