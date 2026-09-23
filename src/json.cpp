#include "json.hpp"

#include <cmath>
#include <fstream>
#include <iomanip>
#include <sstream>

namespace pgo {

JsonError::JsonError(const std::string& msg, int line, int col)
    : std::runtime_error(msg), line_(line), col_(col) {}

double JsonValue::asNumber() const {
  if (type_ != Type::Number) throw JsonError("expected number");
  return num_;
}

const std::string& JsonValue::asString() const {
  if (type_ != Type::String) throw JsonError("expected string");
  return str_;
}

bool JsonValue::asBool() const {
  if (type_ != Type::Bool) throw JsonError("expected boolean");
  return bool_;
}

const std::vector<JsonValue>& JsonValue::asArray() const {
  if (type_ != Type::Array) throw JsonError("expected array");
  return arr_;
}

const std::map<std::string, JsonValue>& JsonValue::asObject() const {
  if (type_ != Type::Object) throw JsonError("expected object");
  return obj_;
}

bool JsonValue::contains(const std::string& key) const {
  return type_ == Type::Object && obj_.count(key) > 0;
}

const JsonValue& JsonValue::at(const std::string& key) const {
  if (type_ != Type::Object) throw JsonError("expected object");
  auto it = obj_.find(key);
  if (it == obj_.end()) throw JsonError("missing key: " + key);
  return it->second;
}

const JsonValue& JsonValue::find(const std::string& key) const {
  static const JsonValue nullValue;
  if (type_ != Type::Object) return nullValue;
  auto it = obj_.find(key);
  return it == obj_.end() ? nullValue : it->second;
}

void JsonValue::set(const std::string& key, JsonValue v) {
  if (type_ != Type::Object) throw JsonError("set() on non-object");
  obj_[key] = std::move(v);
}

void JsonValue::push(JsonValue v) {
  if (type_ != Type::Array) throw JsonError("push() on non-array");
  arr_.push_back(std::move(v));
}

namespace {

class Parser {
 public:
  explicit Parser(const std::string& text) : s_(text) {}

  JsonValue parse() {
    skipWs();
    JsonValue v = parseValue();
    skipWs();
    if (pos_ != s_.size()) fail("trailing characters after JSON document");
    return v;
  }

 private:
  const std::string& s_;
  size_t pos_ = 0;

  [[noreturn]] void fail(const std::string& msg) {
    int line = 1, col = 1;
    for (size_t i = 0; i < pos_ && i < s_.size(); ++i) {
      if (s_[i] == '\n') { ++line; col = 1; } else { ++col; }
    }
    throw JsonError(msg, line, col);
  }

  void skipWs() {
    while (pos_ < s_.size()) {
      char c = s_[pos_];
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') { ++pos_; } else { break; }
    }
  }

  char peek() {
    if (pos_ >= s_.size()) fail("unexpected end of input");
    return s_[pos_];
  }

  void expect(char c) {
    if (pos_ >= s_.size() || s_[pos_] != c) fail(std::string("expected '") + c + "'");
    ++pos_;
  }

  bool consumeLit(const char* lit) {
    size_t n = 0;
    while (lit[n]) ++n;
    if (s_.compare(pos_, n, lit) == 0) { pos_ += n; return true; }
    return false;
  }

  JsonValue parseValue() {
    skipWs();
    if (pos_ >= s_.size()) fail("unexpected end of input");
    char c = s_[pos_];
    switch (c) {
      case '{': return parseObject();
      case '[': return parseArray();
      case '"': return JsonValue::makeString(parseString());
      case 't': case 'f': {
        if (consumeLit("true")) return JsonValue::makeBool(true);
        if (consumeLit("false")) return JsonValue::makeBool(false);
        fail("invalid literal");
      }
      case 'n': {
        if (consumeLit("null")) return JsonValue();
        fail("invalid literal");
      }
      default:
        if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
        fail(std::string("unexpected character '") + c + "'");
    }
  }

  JsonValue parseObject() {
    expect('{');
    JsonValue obj = JsonValue::makeObject();
    skipWs();
    if (peek() == '}') { ++pos_; return obj; }
    while (true) {
      skipWs();
      if (peek() != '"') fail("expected string key in object");
      std::string key = parseString();
      skipWs();
      expect(':');
      JsonValue val = parseValue();
      obj.set(key, std::move(val));
      skipWs();
      char c = s_[pos_++];
      if (c == ',') continue;
      if (c == '}') break;
      fail("expected ',' or '}' in object");
    }
    return obj;
  }

  JsonValue parseArray() {
    expect('[');
    JsonValue arr = JsonValue::makeArray();
    skipWs();
    if (pos_ < s_.size() && peek() == ']') { ++pos_; return arr; }
    while (true) {
      arr.push(parseValue());
      skipWs();
      if (pos_ >= s_.size()) fail("unterminated array");
      char c = s_[pos_++];
      if (c == ',') continue;
      if (c == ']') break;
      fail("expected ',' or ']' in array");
    }
    return arr;
  }

  std::string parseString() {
    expect('"');
    std::string out;
    while (true) {
      if (pos_ >= s_.size()) fail("unterminated string");
      char c = s_[pos_++];
      if (c == '"') break;
      if (c == '\\') {
        if (pos_ >= s_.size()) fail("unterminated escape");
        char e = s_[pos_++];
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
            unsigned cp = parseHex4();
            if (cp >= 0xD800 && cp <= 0xDBFF) {
              // High surrogate; must be followed by low surrogate.
              if (pos_ + 5 < s_.size() && s_[pos_] == '\\' && s_[pos_ + 1] == 'u') {
                pos_ += 2;
                unsigned lo = parseHex4();
                if (lo >= 0xDC00 && lo <= 0xDFFF) {
                  cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                } else {
                  fail("invalid UTF-16 surrogate pair");
                }
              } else {
                fail("expected low surrogate");
              }
            } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
              fail("unexpected low surrogate");
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

  unsigned parseHex4() {
    if (pos_ + 4 > s_.size()) fail("incomplete \\u escape");
    unsigned v = 0;
    for (int i = 0; i < 4; ++i) {
      char c = s_[pos_++];
      v <<= 4;
      if (c >= '0' && c <= '9') v |= static_cast<unsigned>(c - '0');
      else if (c >= 'a' && c <= 'f') v |= static_cast<unsigned>(c - 'a' + 10);
      else if (c >= 'A' && c <= 'F') v |= static_cast<unsigned>(c - 'A' + 10);
      else fail("invalid hex digit in \\u escape");
    }
    return v;
  }

  static void appendUtf8(std::string& out, unsigned cp) {
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

  JsonValue parseNumber() {
    size_t start = pos_;
    if (peek() == '-') ++pos_;
    if (pos_ >= s_.size()) fail("invalid number");
    if (s_[pos_] == '0') {
      ++pos_;
    } else if (s_[pos_] >= '1' && s_[pos_] <= '9') {
      while (pos_ < s_.size() && s_[pos_] >= '0' && s_[pos_] <= '9') ++pos_;
    } else {
      fail("invalid number");
    }
    if (pos_ < s_.size() && s_[pos_] == '.') {
      ++pos_;
      if (pos_ >= s_.size() || s_[pos_] < '0' || s_[pos_] > '9') fail("invalid fraction");
      while (pos_ < s_.size() && s_[pos_] >= '0' && s_[pos_] <= '9') ++pos_;
    }
    if (pos_ < s_.size() && (s_[pos_] == 'e' || s_[pos_] == 'E')) {
      ++pos_;
      if (pos_ < s_.size() && (s_[pos_] == '+' || s_[pos_] == '-')) ++pos_;
      if (pos_ >= s_.size() || s_[pos_] < '0' || s_[pos_] > '9') fail("invalid exponent");
      while (pos_ < s_.size() && s_[pos_] >= '0' && s_[pos_] <= '9') ++pos_;
    }
    std::string token = s_.substr(start, pos_ - start);
    try {
      return JsonValue::makeNumber(std::stod(token));
    } catch (...) {
      fail("number out of range");
    }
  }
};

std::string escapeString(const std::string& s) {
  std::string out;
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
  return out;
}

}  // namespace

JsonValue parseJson(const std::string& text) { return Parser(text).parse(); }

JsonValue loadJsonFile(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) throw JsonError("cannot open file: " + path);
  std::ostringstream ss;
  ss << f.rdbuf();
  if (!f && !ss.eof()) throw JsonError("cannot read file: " + path);
  return parseJson(ss.str());
}

void JsonValue::dumpTo(std::string& out, int indent, int depth) const {
  auto pad = [&](int d) {
    if (indent > 0) {
      out.push_back('\n');
      out.append(static_cast<size_t>(indent) * d, ' ');
    }
  };
  switch (type_) {
    case Type::Null: out += "null"; break;
    case Type::Bool: out += bool_ ? "true" : "false"; break;
    case Type::Number: {
      if (std::isfinite(num_)) {
        std::ostringstream ss;
        ss << std::setprecision(17) << num_;
        out += ss.str();
      } else {
        // JSON has no NaN/Inf; emit null rather than corrupt output.
        out += "null";
      }
      break;
    }
    case Type::String: out += escapeString(str_); break;
    case Type::Array: {
      if (arr_.empty()) { out += "[]"; break; }
      out.push_back('[');
      for (size_t i = 0; i < arr_.size(); ++i) {
        if (i) out += indent > 0 ? "," : ",";
        pad(depth + 1);
        arr_[i].dumpTo(out, indent, depth + 1);
      }
      pad(depth);
      out.push_back(']');
      break;
    }
    case Type::Object: {
      if (obj_.empty()) { out += "{}"; break; }
      out.push_back('{');
      size_t i = 0;
      for (const auto& kv : obj_) {
        if (i++) out += indent > 0 ? "," : ",";
        pad(depth + 1);
        out += escapeString(kv.first);
        out += indent > 0 ? ": " : ":";
        kv.second.dumpTo(out, indent, depth + 1);
      }
      pad(depth);
      out.push_back('}');
      break;
    }
  }
}

std::string JsonValue::dump(int indent) const {
  std::string out;
  dumpTo(out, indent, 0);
  return out;
}

void saveJsonFile(const std::string& path, const JsonValue& value, int indent) {
  std::ofstream f(path, std::ios::binary | std::ios::trunc);
  if (!f) throw JsonError("cannot write file: " + path);
  std::string text = value.dump(indent);
  f.write(text.data(), static_cast<std::streamsize>(text.size()));
  if (!f) throw JsonError("write failed: " + path);
}

}  // namespace pgo
