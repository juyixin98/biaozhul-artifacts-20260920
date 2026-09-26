// json.hpp - Minimal zero-dependency JSON parser/serializer.
// Supports: null, bool, int64, double, string (UTF-8), array, ordered object.
// Not a general-purpose library: only what the difference-constraints service needs.
#ifndef DIFFCON_JSON_HPP
#define DIFFCON_JSON_HPP

#include <cstdint>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace json {

class Value;
using JsonArray = std::vector<Value>;
using JsonObject = std::vector<std::pair<std::string, Value>>;  // insertion order preserved

enum class Type { Null, Bool, Int, Double, String, Array, Object };

class ParseError : public std::runtime_error {
 public:
  explicit ParseError(const std::string& msg) : std::runtime_error(msg) {}
};

class Value {
 public:
  Value() : type_(Type::Null), bool_(false), int_(0), double_(0.0) {}
  Value(std::nullptr_t) : Value() {}  // NOLINT(runtime/explicit)
  Value(bool b) : type_(Type::Bool), bool_(b), int_(0), double_(0.0) {}  // NOLINT
  Value(int64_t v) : type_(Type::Int), bool_(false), int_(v), double_(0.0) {}  // NOLINT
  Value(int v) : Value(static_cast<int64_t>(v)) {}  // NOLINT
  Value(double v) : type_(Type::Double), bool_(false), int_(0), double_(v) {}  // NOLINT
  Value(const char* s) : type_(Type::String), bool_(false), int_(0), double_(0.0), str_(s) {}  // NOLINT
  Value(std::string s) : type_(Type::String), bool_(false), int_(0), double_(0.0), str_(std::move(s)) {}  // NOLINT
  Value(JsonArray a) : type_(Type::Array), bool_(false), int_(0), double_(0.0), arr_(std::move(a)) {}  // NOLINT
  Value(JsonObject o) : type_(Type::Object), bool_(false), int_(0), double_(0.0), obj_(std::move(o)) {}  // NOLINT

  Type type() const { return type_; }
  bool isNull() const { return type_ == Type::Null; }
  bool isBool() const { return type_ == Type::Bool; }
  bool isInt() const { return type_ == Type::Int; }
  bool isNumber() const { return type_ == Type::Int || type_ == Type::Double; }
  bool isString() const { return type_ == Type::String; }
  bool isArray() const { return type_ == Type::Array; }
  bool isObject() const { return type_ == Type::Object; }

  bool asBool() const { check(Type::Bool); return bool_; }
  int64_t asInt() const {
    if (type_ == Type::Int) return int_;
    if (type_ == Type::Double) return static_cast<int64_t>(double_);
    throw ParseError("expected integer");
  }
  double asDouble() const {
    if (type_ == Type::Double) return double_;
    if (type_ == Type::Int) return static_cast<double>(int_);
    throw ParseError("expected number");
  }
  const std::string& asString() const { check(Type::String); return str_; }
  const JsonArray& asArray() const { check(Type::Array); return arr_; }
  const JsonObject& asObject() const { check(Type::Object); return obj_; }

  // Object lookup (linear scan; request objects are tiny). Returns nullptr when absent.
  const Value* find(const std::string& key) const {
    if (type_ != Type::Object) return nullptr;
    for (const auto& kv : obj_) {
      if (kv.first == key) return &kv.second;
    }
    return nullptr;
  }
  bool contains(const std::string& key) const { return find(key) != nullptr; }

  // Mutable builders for arrays/objects.
  static Value makeObject() { Value v; v.type_ = Type::Object; return v; }
  static Value makeArray() { Value v; v.type_ = Type::Array; return v; }
  void pushBack(Value v) { check(Type::Array); arr_.push_back(std::move(v)); }
  void set(const std::string& key, Value v) {
    if (type_ != Type::Object) throw ParseError("set() on non-object");
    for (auto& kv : obj_) {
      if (kv.first == key) { kv.second = std::move(v); return; }
    }
    obj_.emplace_back(key, std::move(v));
  }

  // ---- Parsing ----
  static Value parse(const std::string& text) {
    Parser p(text);
    p.skipWs();
    Value v = p.parseValue();
    p.skipWs();
    if (p.pos != p.src.size()) throw ParseError("trailing characters after JSON value");
    return v;
  }

  // ---- Serialization ----
  std::string dump(int indent = 2) const {
    std::ostringstream out;
    write(out, indent, 0);
    out << '\n';
    return out.str();
  }

 private:
  Type type_;
  bool bool_;
  int64_t int_;
  double double_;
  std::string str_;
  JsonArray arr_;
  JsonObject obj_;

  void check(Type t) const {
    if (type_ != t) throw ParseError("JSON value has unexpected type");
  }

  // ---------------- Parser ----------------
  class Parser {
   public:
    explicit Parser(const std::string& s) : src(s), pos(0) {}
    const std::string& src;
    size_t pos;

    void skipWs() {
      while (pos < src.size() && (src[pos] == ' ' || src[pos] == '\t' || src[pos] == '\n' || src[pos] == '\r'))
        ++pos;
    }

    Value parseValue() {
      if (pos >= src.size()) throw ParseError("unexpected end of input");
      char c = src[pos];
      switch (c) {
        case '{': return parseObject();
        case '[': return parseArray();
        case '"': return Value(parseString());
        case 't': case 'f': return parseBool();
        case 'n': return parseNull();
        default:
          if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
          throw ParseError("unexpected character '" + std::string(1, c) + "'");
      }
    }

    Value parseObject() {
      JsonObject obj;
      ++pos;  // '{'
      skipWs();
      if (pos < src.size() && src[pos] == '}') { ++pos; return Value(std::move(obj)); }
      while (true) {
        skipWs();
        if (pos >= src.size() || src[pos] != '"') throw ParseError("expected string key in object");
        std::string key = parseString();
        skipWs();
        if (pos >= src.size() || src[pos] != ':') throw ParseError("expected ':' after object key");
        ++pos;
        skipWs();
        obj.emplace_back(std::move(key), parseValue());
        skipWs();
        if (pos >= src.size()) throw ParseError("unterminated object");
        if (src[pos] == ',') { ++pos; continue; }
        if (src[pos] == '}') { ++pos; break; }
        throw ParseError("expected ',' or '}' in object");
      }
      return Value(std::move(obj));
    }

    Value parseArray() {
      JsonArray arr;
      ++pos;  // '['
      skipWs();
      if (pos < src.size() && src[pos] == ']') { ++pos; return Value(std::move(arr)); }
      while (true) {
        skipWs();
        arr.push_back(parseValue());
        skipWs();
        if (pos >= src.size()) throw ParseError("unterminated array");
        if (src[pos] == ',') { ++pos; continue; }
        if (src[pos] == ']') { ++pos; break; }
        throw ParseError("expected ',' or ']' in array");
      }
      return Value(std::move(arr));
    }

    Value parseBool() {
      if (src.compare(pos, 4, "true") == 0) { pos += 4; return Value(true); }
      if (src.compare(pos, 5, "false") == 0) { pos += 5; return Value(false); }
      throw ParseError("invalid literal");
    }

    Value parseNull() {
      if (src.compare(pos, 4, "null") == 0) { pos += 4; return Value(); }
      throw ParseError("invalid literal");
    }

    Value parseNumber() {
      size_t start = pos;
      bool isDouble = false;
      if (src[pos] == '-') ++pos;
      while (pos < src.size()) {
        char c = src[pos];
        if (c >= '0' && c <= '9') { ++pos; }
        else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') { isDouble = true; ++pos; }
        else break;
      }
      std::string token = src.substr(start, pos - start);
      try {
        if (isDouble) return Value(std::stod(token));
        return Value(static_cast<int64_t>(std::stoll(token)));
      } catch (const std::exception&) {
        throw ParseError("invalid number: " + token);
      }
    }

    std::string parseString() {
      ++pos;  // opening quote
      std::string result;
      while (pos < src.size()) {
        unsigned char c = static_cast<unsigned char>(src[pos++]);
        if (c == '"') return result;
        if (c == '\\') {
          if (pos >= src.size()) throw ParseError("unterminated escape");
          char e = src[pos++];
          switch (e) {
            case '"': result.push_back('"'); break;
            case '\\': result.push_back('\\'); break;
            case '/': result.push_back('/'); break;
            case 'b': result.push_back('\b'); break;
            case 'f': result.push_back('\f'); break;
            case 'n': result.push_back('\n'); break;
            case 'r': result.push_back('\r'); break;
            case 't': result.push_back('\t'); break;
            case 'u': {
              uint32_t cp = parseHex4();
              if (cp >= 0xD800 && cp <= 0xDBFF) {
                if (pos + 1 < src.size() && src[pos] == '\\' && src[pos + 1] == 'u') {
                  pos += 2;
                  uint32_t lo = parseHex4();
                  if (lo >= 0xDC00 && lo <= 0xDFFF)
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                  else
                    throw ParseError("invalid low surrogate");
                } else {
                  throw ParseError("unpaired high surrogate");
                }
              }
              appendUtf8(result, cp);
              break;
            }
            default: throw ParseError(std::string("invalid escape \\") + e);
          }
        } else if (c < 0x20) {
          throw ParseError("unescaped control character in string");
        } else {
          result.push_back(static_cast<char>(c));
        }
      }
      throw ParseError("unterminated string");
    }

    uint32_t parseHex4() {
      if (pos + 4 > src.size()) throw ParseError("incomplete \\u escape");
      uint32_t cp = 0;
      for (int i = 0; i < 4; ++i) {
        char h = src[pos++];
        cp <<= 4;
        if (h >= '0' && h <= '9') cp += static_cast<uint32_t>(h - '0');
        else if (h >= 'a' && h <= 'f') cp += static_cast<uint32_t>(h - 'a' + 10);
        else if (h >= 'A' && h <= 'F') cp += static_cast<uint32_t>(h - 'A' + 10);
        else throw ParseError("invalid hex digit in \\u escape");
      }
      return cp;
    }

    static void appendUtf8(std::string& out, uint32_t cp) {
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
  };

  // ---------------- Serializer ----------------
  static void escapeString(std::ostringstream& out, const std::string& s) {
    out << '"';
    for (char raw : s) {
      unsigned char c = static_cast<unsigned char>(raw);
      switch (c) {
        case '"': out << "\\\""; break;
        case '\\': out << "\\\\"; break;
        case '\b': out << "\\b"; break;
        case '\f': out << "\\f"; break;
        case '\n': out << "\\n"; break;
        case '\r': out << "\\r"; break;
        case '\t': out << "\\t"; break;
        default:
          if (c < 0x20) {
            static const char* hex = "0123456789abcdef";
            out << "\\u00" << hex[(c >> 4) & 0xF] << hex[c & 0xF];
          } else {
            out << static_cast<char>(c);
          }
      }
    }
    out << '"';
  }

  void write(std::ostringstream& out, int indent, int depth) const {
    switch (type_) {
      case Type::Null: out << "null"; break;
      case Type::Bool: out << (bool_ ? "true" : "false"); break;
      case Type::Int: out << int_; break;
      case Type::Double: {
        std::ostringstream tmp;
        tmp.precision(17);
        tmp << double_;
        out << tmp.str();
        break;
      }
      case Type::String: escapeString(out, str_); break;
      case Type::Array: {
        if (arr_.empty()) { out << "[]"; break; }
        out << '[';
        bool first = true;
        for (const auto& v : arr_) {
          if (!first) out << ',';
          first = false;
          if (indent > 0) { out << '\n'; pad(out, indent, depth + 1); }
          v.write(out, indent, depth + 1);
        }
        if (indent > 0) { out << '\n'; pad(out, indent, depth); }
        out << ']';
        break;
      }
      case Type::Object: {
        if (obj_.empty()) { out << "{}"; break; }
        out << '{';
        bool first = true;
        for (const auto& kv : obj_) {
          if (!first) out << ',';
          first = false;
          if (indent > 0) { out << '\n'; pad(out, indent, depth + 1); }
          escapeString(out, kv.first);
          out << (indent > 0 ? ": " : ":");
          kv.second.write(out, indent, depth + 1);
        }
        if (indent > 0) { out << '\n'; pad(out, indent, depth); }
        out << '}';
        break;
      }
    }
  }

  static void pad(std::ostringstream& out, int indent, int depth) {
    for (int i = 0; i < indent * depth; ++i) out << ' ';
  }
};

}  // namespace json

#endif  // DIFFCON_JSON_HPP
