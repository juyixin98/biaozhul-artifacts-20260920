#pragma once
//
// json.hpp — minimal, dependency-free JSON parser and writer.
//
// Supports objects, arrays, strings, numbers, true/false/null. Numbers are
// stored as int64 when they fit (integral), otherwise double; the application
// here only accepts integer fields and rejects fractions/overflow explicitly.
//
#include <cstdint>
#include <map>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

namespace ru::json {

class Value;
using Object = std::map<std::string, Value>;

enum class Type { Null, Bool, Int, Double, String, Array, Object_ };

class Value {
public:
    Value() : type_(Type::Null) {}
    Value(std::nullptr_t) : type_(Type::Null) {}
    Value(bool b) : type_(Type::Bool), b_(b) {}
    Value(int v) : type_(Type::Int), i_(v) {}
    Value(long long v) : type_(Type::Int), i_(v) {}
    Value(double d) : type_(Type::Double), d_(d) {}
    Value(const char* s) : type_(Type::String), s_(s) {}
    Value(std::string s) : type_(Type::String), s_(std::move(s)) {}

    static Value array() { Value v; v.type_ = Type::Array; v.a_ = std::make_shared<std::vector<Value>>(); return v; }
    static Value object() { Value v; v.type_ = Type::Object_; v.o_ = std::make_shared<Object>(); return v; }

    Type type() const { return type_; }
    bool isNull() const { return type_ == Type::Null; }
    bool isBool() const { return type_ == Type::Bool; }
    bool isInt() const { return type_ == Type::Int; }
    bool isNumber() const { return type_ == Type::Int || type_ == Type::Double; }
    bool isString() const { return type_ == Type::String; }
    bool isArray() const { return type_ == Type::Array; }
    bool isObject() const { return type_ == Type::Object_; }

    bool asBool() const { return b_; }
    long long asInt() const { return type_ == Type::Int ? i_ : static_cast<long long>(d_); }
    double asDouble() const { return type_ == Type::Double ? d_ : static_cast<double>(i_); }
    const std::string& asString() const { return s_; }

    std::vector<Value>& arr() { return *a_; }
    const std::vector<Value>& arr() const { return *a_; }
    Object& obj() { return *o_; }
    const Object& obj() const { return *o_; }

    bool has(const std::string& key) const {
        return type_ == Type::Object_ && o_->find(key) != o_->end();
    }
    const Value& at(const std::string& key) const {
        static const Value nullValue;
        auto it = o_->find(key);
        return it == o_->end() ? nullValue : it->second;
    }
    void set(const std::string& key, Value v) { (*o_)[key] = std::move(v); }
    void push(Value v) { a_->push_back(std::move(v)); }

private:
    Type type_;
    bool b_ = false;
    long long i_ = 0;
    double d_ = 0.0;
    std::string s_;
    std::shared_ptr<std::vector<Value>> a_;
    std::shared_ptr<Object> o_;
};

class ParseError : public std::runtime_error {
public:
    ParseError(const std::string& msg, size_t pos)
        : std::runtime_error(msg + " (at byte " + std::to_string(pos) + ")") {}
};

class Parser {
public:
    explicit Parser(const std::string& text) : s_(text) {}

    Value parse() {
        Value v = parseValue();
        skipWs();
        if (pos_ != s_.size()) fail("trailing characters after JSON value");
        return v;
    }

private:
    const std::string& s_;
    size_t pos_ = 0;
    int depth_ = 0;

    [[noreturn]] void fail(const std::string& msg) { throw ParseError(msg, pos_); }

    void skipWs() {
        while (pos_ < s_.size()) {
            char c = s_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos_;
            else break;
        }
    }

    char peek() {
        if (pos_ >= s_.size()) fail("unexpected end of input");
        return s_[pos_];
    }

    char get() {
        if (pos_ >= s_.size()) fail("unexpected end of input");
        return s_[pos_++];
    }

    void expect(char c) {
        if (get() != c) fail(std::string("expected '") + c + "'");
    }

    Value parseValue() {
        skipWs();
        if (pos_ >= s_.size()) fail("unexpected end of input");
        char c = s_[pos_];
        switch (c) {
            case '{': return parseObject();
            case '[': return parseArray();
            case '"': return Value(parseString());
            case 't': return parseLiteral("true", Value(true));
            case 'f': return parseLiteral("false", Value(false));
            case 'n': return parseLiteral("null", Value());
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
                fail("unexpected character");
        }
    }

    Value parseLiteral(const char* lit, Value v) {
        for (const char* p = lit; *p; ++p) {
            if (pos_ >= s_.size() || s_[pos_] != *p) fail(std::string("invalid literal"));
            ++pos_;
        }
        return v;
    }

    Value parseObject() {
        if (++depth_ > 256) fail("nesting too deep");
        expect('{');
        Value v = Value::object();
        skipWs();
        if (peek() == '}') { get(); --depth_; return v; }
        while (true) {
            skipWs();
            if (peek() != '"') fail("expected string key");
            std::string key = parseString();
            skipWs();
            expect(':');
            v.set(key, parseValue());
            skipWs();
            char c = get();
            if (c == '}') break;
            if (c != ',') fail("expected ',' or '}'");
        }
        --depth_;
        return v;
    }

    Value parseArray() {
        if (++depth_ > 256) fail("nesting too deep");
        expect('[');
        Value v = Value::array();
        skipWs();
        if (peek() == ']') { get(); --depth_; return v; }
        while (true) {
            v.push(parseValue());
            skipWs();
            char c = get();
            if (c == ']') break;
            if (c != ',') fail("expected ',' or ']'");
        }
        --depth_;
        return v;
    }

    std::string parseString() {
        expect('"');
        std::string out;
        while (true) {
            if (pos_ >= s_.size()) fail("unterminated string");
            char c = s_[pos_++];
            if (c == '"') break;
            if (static_cast<unsigned char>(c) < 0x20) fail("unescaped control character in string");
            if (c != '\\') { out.push_back(c); continue; }
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
                    unsigned cp = parseHex4();
                    if (cp >= 0xD800 && cp <= 0xDBFF) {
                        if (pos_ + 1 < s_.size() && s_[pos_] == '\\' && s_[pos_ + 1] == 'u') {
                            pos_ += 2;
                            unsigned lo = parseHex4();
                            if (lo >= 0xDC00 && lo <= 0xDFFF)
                                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                            else
                                fail("invalid UTF-16 surrogate pair");
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
        }
        return out;
    }

    unsigned parseHex4() {
        if (pos_ + 4 > s_.size()) fail("incomplete \\u escape");
        unsigned cp = 0;
        for (int k = 0; k < 4; ++k) {
            char c = s_[pos_++];
            cp <<= 4;
            if (c >= '0' && c <= '9') cp |= static_cast<unsigned>(c - '0');
            else if (c >= 'a' && c <= 'f') cp |= static_cast<unsigned>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') cp |= static_cast<unsigned>(c - 'A' + 10);
            else fail("invalid hex digit in \\u escape");
        }
        return cp;
    }

    static void appendUtf8(std::string& out, unsigned cp) {
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

    Value parseNumber() {
        size_t start = pos_;
        if (peek() == '-') get();
        if (pos_ >= s_.size()) fail("invalid number");
        if (s_[pos_] == '0') {
            ++pos_;
        } else if (s_[pos_] >= '1' && s_[pos_] <= '9') {
            while (pos_ < s_.size() && s_[pos_] >= '0' && s_[pos_] <= '9') ++pos_;
        } else {
            fail("invalid number");
        }
        bool isDouble = false;
        if (pos_ < s_.size() && s_[pos_] == '.') {
            isDouble = true;
            ++pos_;
            if (pos_ >= s_.size() || s_[pos_] < '0' || s_[pos_] > '9') fail("invalid fraction");
            while (pos_ < s_.size() && s_[pos_] >= '0' && s_[pos_] <= '9') ++pos_;
        }
        if (pos_ < s_.size() && (s_[pos_] == 'e' || s_[pos_] == 'E')) {
            isDouble = true;
            ++pos_;
            if (pos_ < s_.size() && (s_[pos_] == '+' || s_[pos_] == '-')) ++pos_;
            if (pos_ >= s_.size() || s_[pos_] < '0' || s_[pos_] > '9') fail("invalid exponent");
            while (pos_ < s_.size() && s_[pos_] >= '0' && s_[pos_] <= '9') ++pos_;
        }
        std::string tok = s_.substr(start, pos_ - start);
        if (isDouble) return Value(std::stod(tok));
        // strict int64 parse
        bool neg = tok[0] == '-';
        size_t k = neg ? 1 : 0;
        unsigned long long mag = 0;
        for (; k < tok.size(); ++k) {
            unsigned d = static_cast<unsigned>(tok[k] - '0');
            if (mag > (static_cast<unsigned long long>(9223372036854775808ULL) - d) / 10)
                fail("integer out of int64 range");
            mag = mag * 10 + d;
        }
        if (!neg && mag > 9223372036854775807ULL) fail("integer out of int64 range");
        long long v = neg ? -static_cast<long long>(mag) : static_cast<long long>(mag);
        return Value(v);
    }
};

inline Value parse(const std::string& text) {
    Parser p(text);
    return p.parse();
}

inline void dumpTo(std::string& out, const Value& v) {
    switch (v.type()) {
        case Type::Null: out += "null"; break;
        case Type::Bool: out += v.asBool() ? "true" : "false"; break;
        case Type::Int: out += std::to_string(v.asInt()); break;
        case Type::Double: {
            std::ostringstream ss;
            ss.precision(17);
            ss << v.asDouble();
            out += ss.str();
            break;
        }
        case Type::String: {
            out.push_back('"');
            for (char raw : v.asString()) {
                unsigned char c = static_cast<unsigned char>(raw);
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
            break;
        }
        case Type::Array: {
            out.push_back('[');
            bool first = true;
            for (const Value& e : v.arr()) {
                if (!first) out.push_back(',');
                first = false;
                dumpTo(out, e);
            }
            out.push_back(']');
            break;
        }
        case Type::Object_: {
            out.push_back('{');
            bool first = true;
            for (const auto& [key, e] : v.obj()) {
                if (!first) out.push_back(',');
                first = false;
                out.push_back('"');
                out += key;
                out += "\":";
                dumpTo(out, e);
            }
            out.push_back('}');
            break;
        }
    }
}

inline std::string dump(const Value& v) {
    std::string out;
    dumpTo(out, v);
    return out;
}

}  // namespace ru::json
