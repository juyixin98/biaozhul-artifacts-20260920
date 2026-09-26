#include "minjson.h"

#include <cmath>
#include <cstdio>
#include <sstream>

namespace minjson {

const Value* Value::find(std::string_view key) const {
  for (const auto& [k, v] : members) {
    if (k == key) return &v;
  }
  return nullptr;
}

Value* Value::find(std::string_view key) {
  for (auto& [k, v] : members) {
    if (k == key) return &v;
  }
  return nullptr;
}

void Value::set(const std::string& key, Value value) {
  if (Value* existing = find(key)) {
    *existing = std::move(value);
    return;
  }
  members.emplace_back(key, std::move(value));
}

namespace {

class Parser {
 public:
  Parser(std::string_view input, ParseError* error)
      : in_(input), pos_(0), error_(error) {}

  std::optional<Value> run() {
    skipWs();
    std::optional<Value> v = parseValue();
    if (!v) return std::nullopt;
    skipWs();
    if (pos_ != in_.size()) return fail("trailing characters after JSON value");
    return v;
  }

 private:
  std::string_view in_;
  std::size_t pos_;
  ParseError* error_;

  [[noreturn]] std::optional<Value> fail(const std::string& msg) {
    if (error_) {
      error_->message = msg;
      error_->offset = pos_;
    }
    throw ParseFailure{};
  }

  struct ParseFailure {};

  char peek() const { return pos_ < in_.size() ? in_[pos_] : '\0'; }
  char get() { return pos_ < in_.size() ? in_[pos_++] : '\0'; }
  bool consume(char c) {
    if (peek() == c) {
      ++pos_;
      return true;
    }
    return false;
  }
  void expect(char c, const char* what) {
    if (!consume(c)) fail(std::string("expected ") + what);
  }

  void skipWs() {
    while (pos_ < in_.size()) {
      char c = in_[pos_];
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
        ++pos_;
      } else {
        break;
      }
    }
  }

  std::optional<Value> parseValue() {
    try {
      skipWs();
      char c = peek();
      switch (c) {
        case '{': return parseObject();
        case '[': return parseArray();
        case '"': return Value::makeString(parseString());
        case 't':
        case 'f': return parseBool();
        case 'n': return parseNull();
        default: return parseNumber();
      }
    } catch (const ParseFailure&) {
      return std::nullopt;
    }
  }

  Value parseObject() {
    expect('{', "'{'");
    Value obj = Value::makeObject();
    skipWs();
    if (consume('}')) return obj;
    while (true) {
      skipWs();
      if (peek() != '"') fail("expected string key in object");
      std::string key = parseString();
      skipWs();
      expect(':', "':'");
      std::optional<Value> v = parseValue();
      if (!v) throw ParseFailure{};
      obj.set(key, std::move(*v));
      skipWs();
      if (consume(',')) continue;
      expect('}', "'}' or ',' in object");
      return obj;
    }
  }

  Value parseArray() {
    expect('[', "'['");
    Value arr = Value::makeArray();
    skipWs();
    if (consume(']')) return arr;
    while (true) {
      std::optional<Value> v = parseValue();
      if (!v) throw ParseFailure{};
      arr.push(std::move(*v));
      skipWs();
      if (consume(',')) continue;
      expect(']', "']' or ',' in array");
      return arr;
    }
  }

  std::string parseString() {
    expect('"', "'\"'");
    std::string out;
    while (true) {
      if (pos_ >= in_.size()) fail("unterminated string");
      char c = get();
      if (c == '"') return out;
      if (c == '\\') {
        if (pos_ >= in_.size()) fail("unterminated escape");
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
            unsigned code = 0;
            for (int i = 0; i < 4; ++i) {
              if (pos_ >= in_.size()) fail("bad unicode escape");
              char h = get();
              code <<= 4;
              if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
              else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
              else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
              else fail("bad hex digit in unicode escape");
            }
            appendUtf8(out, code);
            break;
          }
          default: fail("invalid escape character");
        }
      } else {
        if (static_cast<unsigned char>(c) < 0x20) fail("unescaped control character in string");
        out.push_back(c);
      }
    }
  }

  static void appendUtf8(std::string& out, unsigned code) {
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
  }

  Value parseBool() {
    if (in_.compare(pos_, 4, "true") == 0) {
      pos_ += 4;
      return Value::makeBool(true);
    }
    if (in_.compare(pos_, 5, "false") == 0) {
      pos_ += 5;
      return Value::makeBool(false);
    }
    fail("invalid literal");
  }

  Value parseNull() {
    if (in_.compare(pos_, 4, "null") == 0) {
      pos_ += 4;
      return Value::makeNull();
    }
    fail("invalid literal");
  }

  Value parseNumber() {
    std::size_t start = pos_;
    bool isReal = false;
    if (peek() == '-') ++pos_;
    if (peek() == '0') {
      ++pos_;
    } else if (peek() >= '1' && peek() <= '9') {
      while (peek() >= '0' && peek() <= '9') ++pos_;
    } else {
      fail("invalid number");
    }
    if (peek() == '.') {
      isReal = true;
      ++pos_;
      if (!(peek() >= '0' && peek() <= '9')) fail("invalid fraction");
      while (peek() >= '0' && peek() <= '9') ++pos_;
    }
    if (peek() == 'e' || peek() == 'E') {
      isReal = true;
      ++pos_;
      if (peek() == '+' || peek() == '-') ++pos_;
      if (!(peek() >= '0' && peek() <= '9')) fail("invalid exponent");
      while (peek() >= '0' && peek() <= '9') ++pos_;
    }
    std::string token = std::string(in_.substr(start, pos_ - start));
    Value v;
    if (isReal) {
      v.type = Type::Number;
      v.number = std::stod(token);
    } else {
      v.type = Type::Integer;
      try {
        v.integer = std::stoll(token);
      } catch (...) {
        fail("integer out of range");
      }
    }
    return v;
  }
};

class Dumper {
 public:
  Dumper(const Value& v, int indent) : v_(v), indent_(indent) {}

  std::string run() {
    emit(v_, 0);
    return out_.str();
  }

 private:
  const Value& v_;
  int indent_;
  std::ostringstream out_;

  void pad(int depth) {
    if (indent_ <= 0) return;
    for (int i = 0; i < depth * indent_; ++i) out_ << ' ';
  }

  void emit(const Value& v, int depth) {
    switch (v.type) {
      case Type::Null: out_ << "null"; break;
      case Type::Boolean: out_ << (v.boolean ? "true" : "false"); break;
      case Type::Integer: out_ << v.integer; break;
      case Type::Number: {
        char buf[40];
        std::snprintf(buf, sizeof(buf), "%.17g", v.number);
        out_ << buf;
        break;
      }
      case Type::String: emitString(v.text); break;
      case Type::Array: {
        if (v.items.empty()) {
          out_ << "[]";
          break;
        }
        out_ << '[';
        if (indent_ > 0) out_ << '\n';
        for (std::size_t i = 0; i < v.items.size(); ++i) {
          if (i) out_ << ',' << (indent_ > 0 ? "\n" : "");
          pad(depth + 1);
          emit(v.items[i], depth + 1);
        }
        if (indent_ > 0) out_ << '\n';
        pad(depth);
        out_ << ']';
        break;
      }
      case Type::Object: {
        if (v.members.empty()) {
          out_ << "{}";
          break;
        }
        out_ << '{';
        if (indent_ > 0) out_ << '\n';
        for (std::size_t i = 0; i < v.members.size(); ++i) {
          if (i) out_ << ',' << (indent_ > 0 ? "\n" : "");
          pad(depth + 1);
          emitString(v.members[i].first);
          out_ << (indent_ > 0 ? ": " : ":");
          emit(v.members[i].second, depth + 1);
        }
        if (indent_ > 0) out_ << '\n';
        pad(depth);
        out_ << '}';
        break;
      }
    }
  }

  void emitString(const std::string& s) {
    out_ << '"';
    for (unsigned char c : s) {
      switch (c) {
        case '"': out_ << "\\\""; break;
        case '\\': out_ << "\\\\"; break;
        case '\b': out_ << "\\b"; break;
        case '\f': out_ << "\\f"; break;
        case '\n': out_ << "\\n"; break;
        case '\r': out_ << "\\r"; break;
        case '\t': out_ << "\\t"; break;
        default:
          if (c < 0x20) {
            char buf[8];
            std::snprintf(buf, sizeof(buf), "\\u%04x", c);
            out_ << buf;
          } else {
            out_ << static_cast<char>(c);
          }
      }
    }
    out_ << '"';
  }
};

}  // namespace

std::optional<Value> parse(std::string_view input, ParseError* error) {
  if (error) {
    error->message.clear();
    error->offset = 0;
  }
  return Parser(input, error).run();
}

std::string dump(const Value& value, int indent) {
  return Dumper(value, indent).run();
}

}  // namespace minjson
