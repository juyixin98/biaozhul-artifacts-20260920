#include "json.hpp"

#include <cstdio>
#include <string>

namespace mcut {

bool JsonValue::contains(const std::string& key) const {
  return find(key) != nullptr;
}

const JsonValue* JsonValue::find(const std::string& key) const {
  if (type_ != Type::kObject) return nullptr;
  auto it = o_.find(key);
  return it == o_.end() ? nullptr : &it->second;
}

namespace {

class Parser {
 public:
  explicit Parser(const std::string& text) : text_(text) {}

  JsonValue parse() {
    skip_ws();
    JsonValue root = parse_value();
    skip_ws();
    if (pos_ != text_.size()) {
      fail("trailing characters after JSON document");
    }
    return root;
  }

 private:
  const std::string& text_;
  size_t pos_ = 0;

  [[noreturn]] void fail(const std::string& msg) {
    throw JsonParseError{msg, pos_};
  }

  void fail_if(bool cond, const std::string& msg) {
    if (cond) fail(msg);
  }

  void skip_ws() {
    while (pos_ < text_.size()) {
      char c = text_[pos_];
      if (c != ' ' && c != '\t' && c != '\n' && c != '\r') break;
      ++pos_;
    }
  }

  JsonValue parse_value() {
    skip_ws();
    fail_if(pos_ >= text_.size(), "unexpected end of input");
    char c = text_[pos_];
    if (c == '"') return JsonValue::string(parse_string());
    if (c == '{') return parse_object();
    if (c == '[') return parse_array();
    if (c == 't' || c == 'f') return parse_bool();
    if (c == 'n') return parse_null();
    if (c == '-' || (c >= '0' && c <= '9')) return parse_number();
    fail("unexpected character");
  }

  std::string parse_string() {
    fail_if(text_[pos_] != '"', "expected string");
    ++pos_;
    std::string out;
    while (true) {
      fail_if(pos_ >= text_.size(), "unterminated string");
      char c = text_[pos_++];
      if (c == '"') break;
      if (c == '\\') {
        fail_if(pos_ >= text_.size(), "trailing backslash in string");
        char esc = text_[pos_++];
        switch (esc) {
          case '"': out.push_back('"'); break;
          case '\\': out.push_back('\\'); break;
          case '/': out.push_back('/'); break;
          case 'b': out.push_back('\b'); break;
          case 'f': out.push_back('\f'); break;
          case 'n': out.push_back('\n'); break;
          case 'r': out.push_back('\r'); break;
          case 't': out.push_back('\t'); break;
          case 'u': {
            unsigned code = parse_hex4();
            if (0xD800 <= code && code <= 0xDBFF) {
              // High surrogate: must be followed by \uXXXX low surrogate.
              fail_if(pos_ + 5 >= text_.size() || text_[pos_] != '\\' ||
                          text_[pos_ + 1] != 'u',
                      "unpaired high surrogate");
              pos_ += 2;
              unsigned low = parse_hex4();
              fail_if(low < 0xDC00 || low > 0xDFFF, "bad low surrogate");
              code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
            } else if (0xDC00 <= code && code <= 0xDFFF) {
              fail("unexpected low surrogate");
            }
            append_utf8(out, code);
            break;
          }
          default: fail("unknown escape sequence");
        }
      } else if (static_cast<unsigned char>(c) < 0x20) {
        fail("unescaped control character in string");
      } else {
        out.push_back(c);
      }
    }
    return out;
  }

  // Parses exactly four hex digits (position must be at the first digit).
  unsigned parse_hex4() {
    unsigned code = 0;
    for (int k = 0; k < 4; ++k) {
      fail_if(pos_ >= text_.size(), "bad \\u escape");
      char h = text_[pos_++];
      code <<= 4;
      if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
      else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
      else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
      else fail("bad hex digit in \\u escape");
    }
    return code;
  }

  static void append_utf8(std::string& out, unsigned code) {
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

  JsonValue parse_object() {
    ++pos_;  // consume '{'
    JsonValue::Object obj;
    skip_ws();
    if (pos_ < text_.size() && text_[pos_] == '}') {
      ++pos_;
      return JsonValue::object(std::move(obj));
    }
    while (true) {
      skip_ws();
      fail_if(pos_ >= text_.size() || text_[pos_] != '"',
              "expected key string in object");
      std::string key = parse_string();
      skip_ws();
      fail_if(pos_ >= text_.size() || text_[pos_] != ':',
              "expected ':' after object key");
      ++pos_;
      JsonValue val = parse_value();
      obj.emplace(std::move(key), std::move(val));
      skip_ws();
      fail_if(pos_ >= text_.size(), "unterminated object");
      char sep = text_[pos_++];
      if (sep == '}') break;
      fail_if(sep != ',', "expected ',' or '}' in object");
    }
    return JsonValue::object(std::move(obj));
  }

  JsonValue parse_array() {
    ++pos_;  // consume '['
    JsonValue::Array arr;
    skip_ws();
    if (pos_ < text_.size() && text_[pos_] == ']') {
      ++pos_;
      return JsonValue::array(std::move(arr));
    }
    while (true) {
      JsonValue val = parse_value();
      arr.push_back(std::move(val));
      skip_ws();
      fail_if(pos_ >= text_.size(), "unterminated array");
      char sep = text_[pos_++];
      if (sep == ']') break;
      fail_if(sep != ',', "expected ',' or ']' in array");
    }
    return JsonValue::array(std::move(arr));
  }

  JsonValue parse_bool() {
    if (text_.compare(pos_, 4, "true") == 0) {
      pos_ += 4;
      return JsonValue::boolean(true);
    }
    if (text_.compare(pos_, 5, "false") == 0) {
      pos_ += 5;
      return JsonValue::boolean(false);
    }
    fail("invalid literal");
  }

  JsonValue parse_null() {
    if (text_.compare(pos_, 4, "null") == 0) {
      pos_ += 4;
      return JsonValue::null();
    }
    fail("invalid literal");
  }

  JsonValue parse_number() {
    size_t start = pos_;
    if (pos_ < text_.size() && text_[pos_] == '-') ++pos_;
    fail_if(pos_ >= text_.size(), "bad number");
    if (text_[pos_] == '0') {
      ++pos_;
    } else if (text_[pos_] >= '1' && text_[pos_] <= '9') {
      while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
    } else {
      fail("bad number");
    }

    bool is_double = false;
    if (pos_ < text_.size() && text_[pos_] == '.') {
      is_double = true;
      ++pos_;
      fail_if(pos_ >= text_.size() || text_[pos_] < '0' || text_[pos_] > '9',
              "bad fraction");
      while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
    }
    if (pos_ < text_.size() && (text_[pos_] == 'e' || text_[pos_] == 'E')) {
      is_double = true;
      ++pos_;
      if (pos_ < text_.size() && (text_[pos_] == '+' || text_[pos_] == '-')) ++pos_;
      fail_if(pos_ >= text_.size() || text_[pos_] < '0' || text_[pos_] > '9',
              "bad exponent");
      while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
    }

    std::string num = text_.substr(start, pos_ - start);
    if (is_double) {
      try {
        size_t used = 0;
        return JsonValue::real(std::stod(num, &used));
      } catch (...) {
        fail("bad number");
      }
    }

    // Parse as int64 manually to detect overflow.
    bool neg = num[0] == '-';
    std::uint64_t acc = 0;
    for (size_t i = neg ? 1 : 0; i < num.size(); ++i) {
      acc = acc * 10 + static_cast<unsigned>(num[i] - '0');
      fail_if(acc > static_cast<std::uint64_t>(INT64_MAX) + (neg ? 1ULL : 0ULL),
              "integer overflow");
    }
    std::int64_t value = neg ? -static_cast<std::int64_t>(acc)
                             : static_cast<std::int64_t>(acc);
    return JsonValue::integer(value);
  }
};

void dump_string(std::string& out, const std::string& s) {
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
          char buf[7];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out += buf;
        } else {
          out.push_back(static_cast<char>(c));
        }
    }
  }
  out.push_back('"');
}

void dump_value(std::string& out, const JsonValue& v) {
  switch (v.type()) {
    case JsonValue::Type::kNull: out += "null"; break;
    case JsonValue::Type::kBool: out += v.as_bool() ? "true" : "false"; break;
    case JsonValue::Type::kInt: {
      char buf[24];
      std::snprintf(buf, sizeof(buf), "%lld", static_cast<long long>(v.as_int()));
      out += buf;
      break;
    }
    case JsonValue::Type::kDouble: {
      char buf[40];
      int n = std::snprintf(buf, sizeof(buf), "%.17g", v.as_double());
      out.append(buf, n > 0 ? n : 0);
      break;
    }
    case JsonValue::Type::kString: dump_string(out, v.as_string()); break;
    case JsonValue::Type::kArray: {
      out.push_back('[');
      bool first = true;
      for (const JsonValue& el : v.as_array()) {
        if (!first) out.push_back(',');
        first = false;
        dump_value(out, el);
      }
      out.push_back(']');
      break;
    }
    case JsonValue::Type::kObject: {
      out.push_back('{');
      bool first = true;
      for (const auto& kv : v.as_object()) {
        if (!first) out.push_back(',');
        first = false;
        dump_string(out, kv.first);
        out.push_back(':');
        dump_value(out, kv.second);
      }
      out.push_back('}');
      break;
    }
  }
}

}  // namespace

JsonValue parse_json(const std::string& text) {
  return Parser(text).parse();
}

std::string dump_json(const JsonValue& value) {
  std::string out;
  dump_value(out, value);
  return out;
}

}  // namespace mcut
