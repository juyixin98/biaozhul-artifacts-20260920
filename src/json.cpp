#include "json.hpp"

#include <cstdio>
#include <cstdlib>
#include <sstream>

namespace domjson {
namespace {

class Parser {
 public:
  Parser(const std::string& text, std::string& err)
      : s_(text), pos_(0), err_(err) {}

  bool parse(JsonValue& out) {
    skip_ws();
    if (!parse_value(out)) return false;
    skip_ws();
    if (pos_ != s_.size()) {
      return fail("trailing characters after JSON value");
    }
    return true;
  }

 private:
  const std::string& s_;
  size_t pos_;
  std::string& err_;

  bool fail(const std::string& msg) {
    std::ostringstream oss;
    oss << "JSON parse error at offset " << pos_ << ": " << msg;
    err_ = oss.str();
    return false;
  }

  void skip_ws() {
    while (pos_ < s_.size()) {
      char c = s_[pos_];
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
        ++pos_;
      } else {
        break;
      }
    }
  }

  bool consume_literal(const char* lit) {
    size_t n = 0;
    while (lit[n] != '\0') ++n;
    if (s_.compare(pos_, n, lit) != 0) return false;
    pos_ += n;
    return true;
  }

  bool parse_value(JsonValue& out) {
    if (pos_ >= s_.size()) return fail("unexpected end of input");
    char c = s_[pos_];
    switch (c) {
      case '{':
        return parse_object(out);
      case '[':
        return parse_array(out);
      case '"': {
        std::string t;
        if (!parse_string(t)) return false;
        out = JsonValue::make_string(t);
        return true;
      }
      case 't':
        if (!consume_literal("true")) return fail("invalid literal");
        out = JsonValue::make_bool(true);
        return true;
      case 'f':
        if (!consume_literal("false")) return fail("invalid literal");
        out = JsonValue::make_bool(false);
        return true;
      case 'n':
        if (!consume_literal("null")) return fail("invalid literal");
        out = JsonValue::make_null();
        return true;
      default:
        if (c == '-' || (c >= '0' && c <= '9')) return parse_number(out);
        return fail("unexpected character");
    }
  }

  bool parse_object(JsonValue& out) {
    out = JsonValue::make_object();
    ++pos_;  // '{'
    skip_ws();
    if (pos_ < s_.size() && s_[pos_] == '}') {
      ++pos_;
      return true;
    }
    while (true) {
      skip_ws();
      if (pos_ >= s_.size() || s_[pos_] != '"') {
        return fail("expected string key in object");
      }
      std::string key;
      if (!parse_string(key)) return false;
      skip_ws();
      if (pos_ >= s_.size() || s_[pos_] != ':') {
        return fail("expected ':' after object key");
      }
      ++pos_;
      skip_ws();
      JsonValue val;
      if (!parse_value(val)) return false;
      out.obj[key] = val;
      skip_ws();
      if (pos_ >= s_.size()) return fail("unterminated object");
      char c = s_[pos_++];
      if (c == '}') break;
      if (c != ',') return fail("expected ',' or '}' in object");
    }
    return true;
  }

  bool parse_array(JsonValue& out) {
    out = JsonValue::make_array();
    ++pos_;  // '['
    skip_ws();
    if (pos_ < s_.size() && s_[pos_] == ']') {
      ++pos_;
      return true;
    }
    while (true) {
      skip_ws();
      JsonValue val;
      if (!parse_value(val)) return false;
      out.arr.push_back(val);
      skip_ws();
      if (pos_ >= s_.size()) return fail("unterminated array");
      char c = s_[pos_++];
      if (c == ']') break;
      if (c != ',') return fail("expected ',' or ']' in array");
    }
    return true;
  }

  static void append_utf8(std::string& t, unsigned cp) {
    if (cp <= 0x7F) {
      t.push_back(static_cast<char>(cp));
    } else if (cp <= 0x7FF) {
      t.push_back(static_cast<char>(0xC0 | (cp >> 6)));
      t.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp <= 0xFFFF) {
      t.push_back(static_cast<char>(0xE0 | (cp >> 12)));
      t.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
      t.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
      t.push_back(static_cast<char>(0xF0 | (cp >> 18)));
      t.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
      t.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
      t.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
  }

  bool parse_hex4(unsigned& out) {
    if (pos_ + 4 > s_.size()) return fail("invalid unicode escape");
    out = 0;
    for (int i = 0; i < 4; ++i) {
      char c = s_[pos_++];
      out <<= 4;
      if (c >= '0' && c <= '9') {
        out |= static_cast<unsigned>(c - '0');
      } else if (c >= 'a' && c <= 'f') {
        out |= static_cast<unsigned>(c - 'a' + 10);
      } else if (c >= 'A' && c <= 'F') {
        out |= static_cast<unsigned>(c - 'A' + 10);
      } else {
        return fail("invalid hex digit in unicode escape");
      }
    }
    return true;
  }

  bool parse_string(std::string& out) {
    ++pos_;  // opening quote
    while (true) {
      if (pos_ >= s_.size()) return fail("unterminated string");
      char c = s_[pos_++];
      if (c == '"') break;
      if (c == '\\') {
        if (pos_ >= s_.size()) return fail("unterminated escape");
        char e = s_[pos_++];
        switch (e) {
          case '"':
            out.push_back('"');
            break;
          case '\\':
            out.push_back('\\');
            break;
          case '/':
            out.push_back('/');
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
            unsigned hi = 0;
            if (!parse_hex4(hi)) return false;
            unsigned cp = hi;
            if (hi >= 0xD800 && hi <= 0xDBFF) {
              // High surrogate: must be followed by \uXXXX low surrogate.
              if (pos_ + 2 <= s_.size() && s_[pos_] == '\\' &&
                  s_[pos_ + 1] == 'u') {
                pos_ += 2;
                unsigned lo = 0;
                if (!parse_hex4(lo)) return false;
                if (lo >= 0xDC00 && lo <= 0xDFFF) {
                  cp = 0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00);
                } else {
                  // Not a valid low surrogate: emit replacement char.
                  append_utf8(out, 0xFFFD);
                  cp = lo;  // Reinterpret lone value below.
                  if (cp > 0x7F && !(cp >= 0xDC00 && cp <= 0xDFFF)) {
                    append_utf8(out, cp);
                  } else {
                    append_utf8(out, 0xFFFD);
                  }
                  continue;
                }
              } else {
                append_utf8(out, 0xFFFD);
                continue;
              }
            } else if (hi >= 0xDC00 && hi <= 0xDFFF) {
              append_utf8(out, 0xFFFD);
              continue;
            }
            append_utf8(out, cp);
            break;
          }
          default:
            return fail("invalid escape character");
        }
      } else {
        if (static_cast<unsigned char>(c) < 0x20) {
          return fail("unescaped control character in string");
        }
        out.push_back(c);
      }
    }
    return true;
  }

  bool parse_number(JsonValue& out) {
    size_t start = pos_;
    if (s_[pos_] == '-') ++pos_;
    if (pos_ >= s_.size()) return fail("invalid number");
    if (s_[pos_] == '0') {
      ++pos_;
    } else if (s_[pos_] >= '1' && s_[pos_] <= '9') {
      while (pos_ < s_.size() && s_[pos_] >= '0' && s_[pos_] <= '9') ++pos_;
    } else {
      return fail("invalid number");
    }
    bool is_double = false;
    if (pos_ < s_.size() && s_[pos_] == '.') {
      is_double = true;
      ++pos_;
      if (pos_ >= s_.size() || s_[pos_] < '0' || s_[pos_] > '9') {
        return fail("invalid number: expected digit after '.'");
      }
      while (pos_ < s_.size() && s_[pos_] >= '0' && s_[pos_] <= '9') ++pos_;
    }
    if (pos_ < s_.size() && (s_[pos_] == 'e' || s_[pos_] == 'E')) {
      is_double = true;
      ++pos_;
      if (pos_ < s_.size() && (s_[pos_] == '+' || s_[pos_] == '-')) ++pos_;
      if (pos_ >= s_.size() || s_[pos_] < '0' || s_[pos_] > '9') {
        return fail("invalid number: expected digit in exponent");
      }
      while (pos_ < s_.size() && s_[pos_] >= '0' && s_[pos_] <= '9') ++pos_;
    }
    std::string token = s_.substr(start, pos_ - start);
    if (is_double) {
      out = JsonValue::make_number(std::strtod(token.c_str(), nullptr));
    } else {
      long long ll = std::strtoll(token.c_str(), nullptr, 10);
      out = JsonValue::make_number(static_cast<double>(ll));
    }
    return true;
  }
};

void dump_string(std::string& out, const std::string& s) {
  out.push_back('"');
  for (unsigned char c : s) {
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

void dump_number(std::string& out, double d) {
  // Emit integral values without a decimal point; round-trip otherwise.
  if (d == static_cast<double>(static_cast<long long>(d))) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%lld", static_cast<long long>(d));
    out += buf;
  } else {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%.17g", d);
    out += buf;
  }
}

void dump_value(std::string& out, const JsonValue& v, int indent) {
  switch (v.type) {
    case JsonValue::Type::Null:
      out += "null";
      break;
    case JsonValue::Type::Bool:
      out += v.boolean ? "true" : "false";
      break;
    case JsonValue::Type::Number:
      dump_number(out, v.number);
      break;
    case JsonValue::Type::String:
      dump_string(out, v.str);
      break;
    case JsonValue::Type::Array: {
      if (v.arr.empty()) {
        out += "[]";
        break;
      }
      out += "[\n";
      std::string pad(static_cast<size_t>(indent + 1) * 2, ' ');
      for (size_t i = 0; i < v.arr.size(); ++i) {
        out += pad;
        dump_value(out, v.arr[i], indent + 1);
        if (i + 1 < v.arr.size()) out.push_back(',');
        out.push_back('\n');
      }
      out += std::string(static_cast<size_t>(indent) * 2, ' ');
      out.push_back(']');
      break;
    }
    case JsonValue::Type::Object: {
      if (v.obj.empty()) {
        out += "{}";
        break;
      }
      out += "{\n";
      std::string pad(static_cast<size_t>(indent + 1) * 2, ' ');
      size_t i = 0;
      for (const auto& kv : v.obj) {
        out += pad;
        dump_string(out, kv.first);
        out += ": ";
        dump_value(out, kv.second, indent + 1);
        if (++i < v.obj.size()) out.push_back(',');
        out.push_back('\n');
      }
      out += std::string(static_cast<size_t>(indent) * 2, ' ');
      out.push_back('}');
      break;
    }
  }
}

}  // namespace

bool parse_json(const std::string& text, JsonValue& out, std::string& err) {
  Parser parser(text, err);
  return parser.parse(out);
}

std::string dump_json(const JsonValue& v) {
  std::string out;
  dump_value(out, v, 0);
  out.push_back('\n');
  return out;
}

}  // namespace domjson
