#include "json.h"

#include <cmath>
#include <cstdio>
#include <sstream>

namespace dcjson {

const JsonValue* JsonValue::find(const std::string& key) const {
  for (const auto& kv : obj) {
    if (kv.first == key) return &kv.second;
  }
  return nullptr;
}

JsonValue* JsonValue::find(const std::string& key) {
  for (auto& kv : obj) {
    if (kv.first == key) return &kv.second;
  }
  return nullptr;
}

void JsonValue::set(const std::string& key, JsonValue value) {
  for (auto& kv : obj) {
    if (kv.first == key) {
      kv.second = std::move(value);
      return;
    }
  }
  obj.emplace_back(key, std::move(value));
}

namespace {

void appendEscaped(std::string& out, const std::string& s) {
  out.push_back('"');
  for (char c : s) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\t': out += "\\t"; break;
      case '\r': out += "\\r"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
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

void dumpValue(std::string& out, const JsonValue& v, int step, int depth) {
  switch (v.type) {
    case JsonType::Null: out += "null"; break;
    case JsonType::Bool: out += v.boolean ? "true" : "false"; break;
    case JsonType::Int: out += std::to_string(v.intVal); break;
    case JsonType::Double:
      if (std::isfinite(v.dblVal)) {
        std::ostringstream ss;
        ss << v.dblVal;
        out += ss.str();
      } else {
        out += "null";
      }
      break;
    case JsonType::String: appendEscaped(out, v.str); break;
    case JsonType::Array: {
      if (v.arr.empty()) { out += "[]"; break; }
      out.push_back('[');
      if (step > 0) out.push_back('\n');
      for (size_t i = 0; i < v.arr.size(); ++i) {
        if (step > 0) out.append(static_cast<size_t>(step) * static_cast<size_t>(depth + 1), ' ');
        dumpValue(out, v.arr[i], step, depth + 1);
        if (i + 1 < v.arr.size()) out.push_back(',');
        if (step > 0) out.push_back('\n');
      }
      if (step > 0) {
        out.append(static_cast<size_t>(step) * static_cast<size_t>(depth), ' ');
      }
      out.push_back(']');
      break;
    }
    case JsonType::Object: {
      if (v.obj.empty()) { out += "{}"; break; }
      out.push_back('{');
      if (step > 0) out.push_back('\n');
      for (size_t i = 0; i < v.obj.size(); ++i) {
        if (step > 0) out.append(static_cast<size_t>(step) * static_cast<size_t>(depth + 1), ' ');
        appendEscaped(out, v.obj[i].first);
        out.push_back(':');
        if (step > 0) out.push_back(' ');
        dumpValue(out, v.obj[i].second, step, depth + 1);
        if (i + 1 < v.obj.size()) out.push_back(',');
        if (step > 0) out.push_back('\n');
      }
      if (step > 0) {
        out.append(static_cast<size_t>(step) * static_cast<size_t>(depth), ' ');
      }
      out.push_back('}');
      break;
    }
  }
}

class Parser {
 public:
  explicit Parser(const std::string& text) : text_(text) {}

  ParseResult parse() {
    ParseResult result;
    skipWs();
    JsonValue v;
    if (!parseValue(v)) {
      result.error = error_.empty() ? "invalid JSON" : error_;
      return result;
    }
    skipWs();
    if (pos_ != text_.size()) {
      result.error = "trailing characters at position " + std::to_string(pos_);
      return result;
    }
    result.ok = true;
    result.value = std::move(v);
    return result;
  }

 private:
  void skipWs() {
    while (pos_ < text_.size()) {
      char c = text_[pos_];
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos_;
      else break;
    }
  }

  bool fail(const std::string& msg) {
    if (error_.empty()) error_ = msg + " at position " + std::to_string(pos_);
    return false;
  }

  bool consume(char c) {
    if (pos_ < text_.size() && text_[pos_] == c) { ++pos_; return true; }
    return false;
  }

  bool expect(char c) {
    if (!consume(c)) return fail(std::string("expected '") + c + "'");
    return true;
  }

  bool parseValue(JsonValue& v) {
    skipWs();
    if (pos_ >= text_.size()) return fail("unexpected end of input");
    char c = text_[pos_];
    if (c == '{') return parseObject(v);
    if (c == '[') return parseArray(v);
    if (c == '"') {
      std::string s;
      if (!parseString(s)) return false;
      v = JsonValue::makeString(std::move(s));
      return true;
    }
    if (c == 't') return parseLiteral("true", JsonValue::makeBool(true), v);
    if (c == 'f') return parseLiteral("false", JsonValue::makeBool(false), v);
    if (c == 'n') return parseLiteral("null", JsonValue::makeNull(), v);
    if (c == '-' || (c >= '0' && c <= '9')) return parseNumber(v);
    return fail("unexpected character");
  }

  bool parseLiteral(const char* lit, JsonValue out, JsonValue& v) {
    size_t len = std::char_traits<char>::length(lit);
    if (text_.compare(pos_, len, lit) == 0) {
      pos_ += len;
      v = std::move(out);
      return true;
    }
    return fail(std::string("invalid literal, expected ") + lit);
  }

  bool parseNumber(JsonValue& v) {
    size_t start = pos_;
    if (consume('-')) {}
    if (pos_ >= text_.size()) return fail("invalid number");
    if (consume('0')) {
      // no leading zeros allowed
    } else if (text_[pos_] >= '1' && text_[pos_] <= '9') {
      while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
    } else {
      return fail("invalid number");
    }
    bool isDouble = false;
    if (consume('.')) {
      isDouble = true;
      if (pos_ >= text_.size() || text_[pos_] < '0' || text_[pos_] > '9')
        return fail("invalid fraction");
      while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
    }
    if (pos_ < text_.size() && (text_[pos_] == 'e' || text_[pos_] == 'E')) {
      isDouble = true;
      ++pos_;
      if (consume('+') || consume('-')) {}
      if (pos_ >= text_.size() || text_[pos_] < '0' || text_[pos_] > '9')
        return fail("invalid exponent");
      while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
    }
    std::string token = text_.substr(start, pos_ - start);
    try {
      if (isDouble) {
        v = JsonValue{};
        v.type = JsonType::Double;
        v.dblVal = std::stod(token);
      } else {
        v = JsonValue::makeInt(std::stoll(token));
      }
    } catch (const std::exception&) {
      return fail("number out of range");
    }
    return true;
  }

  bool parseString(std::string& s) {
    if (!expect('"')) return false;
    while (pos_ < text_.size()) {
      char c = text_[pos_++];
      if (c == '"') return true;
      if (c == '\\') {
        if (pos_ >= text_.size()) return fail("unterminated escape");
        char e = text_[pos_++];
        switch (e) {
          case '"': s.push_back('"'); break;
          case '\\': s.push_back('\\'); break;
          case '/': s.push_back('/'); break;
          case 'n': s.push_back('\n'); break;
          case 't': s.push_back('\t'); break;
          case 'r': s.push_back('\r'); break;
          case 'b': s.push_back('\b'); break;
          case 'f': s.push_back('\f'); break;
          case 'u': {
            if (pos_ + 4 > text_.size()) return fail("bad \\u escape");
            unsigned code = 0;
            for (int i = 0; i < 4; ++i) {
              char h = text_[pos_++];
              code <<= 4;
              if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
              else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
              else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
              else return fail("bad hex digit in \\u escape");
            }
            // Encode code point as UTF-8 (surrogate pairs handled minimally).
            if (code >= 0xD800 && code <= 0xDBFF) {
              if (pos_ + 6 <= text_.size() && text_[pos_] == '\\' && text_[pos_ + 1] == 'u') {
                pos_ += 2;
                unsigned lo = 0;
                for (int i = 0; i < 4; ++i) {
                  char h = text_[pos_++];
                  lo <<= 4;
                  if (h >= '0' && h <= '9') lo |= static_cast<unsigned>(h - '0');
                  else if (h >= 'a' && h <= 'f') lo |= static_cast<unsigned>(h - 'a' + 10);
                  else if (h >= 'A' && h <= 'F') lo |= static_cast<unsigned>(h - 'A' + 10);
                  else return fail("bad hex digit in surrogate");
                }
                code = 0x10000 + ((code - 0xD800) << 10) + (lo - 0xDC00);
              }
            }
            if (code < 0x80) {
              s.push_back(static_cast<char>(code));
            } else if (code < 0x800) {
              s.push_back(static_cast<char>(0xC0 | (code >> 6)));
              s.push_back(static_cast<char>(0x80 | (code & 0x3F)));
            } else if (code < 0x10000) {
              s.push_back(static_cast<char>(0xE0 | (code >> 12)));
              s.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
              s.push_back(static_cast<char>(0x80 | (code & 0x3F)));
            } else {
              s.push_back(static_cast<char>(0xF0 | (code >> 18)));
              s.push_back(static_cast<char>(0x80 | ((code >> 12) & 0x3F)));
              s.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
              s.push_back(static_cast<char>(0x80 | (code & 0x3F)));
            }
            break;
          }
          default: return fail("invalid escape character");
        }
      } else if (static_cast<unsigned char>(c) < 0x20) {
        return fail("unescaped control character in string");
      } else {
        s.push_back(c);
      }
    }
    return fail("unterminated string");
  }

  bool parseArray(JsonValue& v) {
    v = JsonValue::makeArray();
    pos_++;  // '['
    skipWs();
    if (consume(']')) return true;
    while (true) {
      JsonValue item;
      if (!parseValue(item)) return false;
      v.arr.push_back(std::move(item));
      skipWs();
      if (consume(',')) { skipWs(); continue; }
      if (consume(']')) return true;
      return fail("expected ',' or ']' in array");
    }
  }

  bool parseObject(JsonValue& v) {
    v = JsonValue::makeObject();
    pos_++;  // '{'
    skipWs();
    if (consume('}')) return true;
    while (true) {
      skipWs();
      std::string key;
      if (!parseString(key)) return false;
      skipWs();
      if (!expect(':')) return false;
      JsonValue item;
      if (!parseValue(item)) return false;
      v.obj.emplace_back(std::move(key), std::move(item));
      skipWs();
      if (consume(',')) continue;
      if (consume('}')) return true;
      return fail("expected ',' or '}' in object");
    }
  }

  const std::string& text_;
  size_t pos_ = 0;
  std::string error_;
};

}  // namespace

ParseResult parse(const std::string& text) {
  Parser parser(text);
  return parser.parse();
}

std::string JsonValue::dump(int indentStep) const {
  std::string out;
  dumpValue(out, *this, indentStep, 0);
  return out;
}

}  // namespace dcjson
