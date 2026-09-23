// Minimal JSON parser/serializer for the trajectory interpolation backend.
// Supports: objects, arrays, strings (with standard escapes), numbers,
// true/false/null. No external dependencies.
#pragma once

#include <cctype>
#include <cmath>
#include <cstdint>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace json {

struct Value;
using Object = std::map<std::string, Value>;
using Array = std::vector<Value>;

struct Value {
    enum class Type { Null, Bool, Number, String, Array, Object };

    Type type = Type::Null;
    bool boolean = false;
    double number = 0.0;
    std::string str;
    Array arr;
    Object obj;

    static Value makeBool(bool b) { Value v; v.type = Type::Bool; v.boolean = b; return v; }
    static Value makeNumber(double n) { Value v; v.type = Type::Number; v.number = n; return v; }
    static Value makeString(std::string s) { Value v; v.type = Type::String; v.str = std::move(s); return v; }
    static Value makeArray(Array a) { Value v; v.type = Type::Array; v.arr = std::move(a); return v; }
    static Value makeObject(Object o) { Value v; v.type = Type::Object; v.obj = std::move(o); return v; }

    bool isNull() const { return type == Type::Null; }
    bool isNumber() const { return type == Type::Number; }
    bool isString() const { return type == Type::String; }
    bool isArray() const { return type == Type::Array; }
    bool isObject() const { return type == Type::Object; }

    const Value* find(const std::string& key) const {
        if (type != Type::Object) return nullptr;
        auto it = obj.find(key);
        return it == obj.end() ? nullptr : &it->second;
    }
};

class ParseError : public std::runtime_error {
public:
    explicit ParseError(const std::string& msg) : std::runtime_error(msg) {}
};

class Parser {
public:
    explicit Parser(const std::string& text) : s_(text), pos_(0) {}

    Value parse() {
        skipWs();
        Value v = parseValue();
        skipWs();
        if (pos_ != s_.size())
            throw ParseError("trailing characters at offset " + std::to_string(pos_));
        return v;
    }

private:
    const std::string& s_;
    size_t pos_;

    [[noreturn]] void fail(const std::string& msg) {
        throw ParseError(msg + " at offset " + std::to_string(pos_));
    }

    void skipWs() {
        while (pos_ < s_.size() && std::isspace(static_cast<unsigned char>(s_[pos_]))) ++pos_;
    }

    char peek() const { return pos_ < s_.size() ? s_[pos_] : '\0'; }

    void expect(char c) {
        if (peek() != c) fail(std::string("expected '") + c + "'");
        ++pos_;
    }

    Value parseValue() {
        skipWs();
        char c = peek();
        switch (c) {
            case '{': return parseObject();
            case '[': return parseArray();
            case '"': return Value::makeString(parseString());
            case 't': expectLiteral("true"); return Value::makeBool(true);
            case 'f': expectLiteral("false"); return Value::makeBool(false);
            case 'n': expectLiteral("null"); return Value();
            default:
                if (c == '-' || std::isdigit(static_cast<unsigned char>(c))) return parseNumber();
                fail("unexpected character");
        }
    }

    void expectLiteral(const char* lit) {
        for (const char* p = lit; *p; ++p) {
            if (peek() != *p) fail("invalid literal");
            ++pos_;
        }
    }

    Value parseObject() {
        expect('{');
        Object o;
        skipWs();
        if (peek() == '}') { ++pos_; return Value::makeObject(std::move(o)); }
        while (true) {
            skipWs();
            if (peek() != '"') fail("expected object key");
            std::string key = parseString();
            skipWs();
            expect(':');
            Value v = parseValue();
            o[std::move(key)] = std::move(v);
            skipWs();
            char c = peek();
            if (c == ',') { ++pos_; continue; }
            if (c == '}') { ++pos_; break; }
            fail("expected ',' or '}'");
        }
        return Value::makeObject(std::move(o));
    }

    Value parseArray() {
        expect('[');
        Array a;
        skipWs();
        if (peek() == ']') { ++pos_; return Value::makeArray(std::move(a)); }
        while (true) {
            a.push_back(parseValue());
            skipWs();
            char c = peek();
            if (c == ',') { ++pos_; continue; }
            if (c == ']') { ++pos_; break; }
            fail("expected ',' or ']'");
        }
        return Value::makeArray(std::move(a));
    }

    std::string parseString() {
        expect('"');
        std::string out;
        while (true) {
            char c = peek();
            if (c == '\0') fail("unterminated string");
            ++pos_;
            if (c == '"') break;
            if (c == '\\') {
                char e = peek();
                ++pos_;
                switch (e) {
                    case '"': out += '"'; break;
                    case '\\': out += '\\'; break;
                    case '/': out += '/'; break;
                    case 'b': out += '\b'; break;
                    case 'f': out += '\f'; break;
                    case 'n': out += '\n'; break;
                    case 'r': out += '\r'; break;
                    case 't': out += '\t'; break;
                    case 'u': {
                        unsigned code = 0;
                        for (int i = 0; i < 4; ++i) {
                            char h = peek();
                            ++pos_;
                            code <<= 4;
                            if (h >= '0' && h <= '9') code += h - '0';
                            else if (h >= 'a' && h <= 'f') code += h - 'a' + 10;
                            else if (h >= 'A' && h <= 'F') code += h - 'A' + 10;
                            else fail("invalid \\u escape");
                        }
                        // Encode as UTF-8 (BMP only; surrogate pairs not needed here).
                        if (code < 0x80) {
                            out += static_cast<char>(code);
                        } else if (code < 0x800) {
                            out += static_cast<char>(0xC0 | (code >> 6));
                            out += static_cast<char>(0x80 | (code & 0x3F));
                        } else {
                            out += static_cast<char>(0xE0 | (code >> 12));
                            out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
                            out += static_cast<char>(0x80 | (code & 0x3F));
                        }
                        break;
                    }
                    default: fail("invalid escape");
                }
            } else {
                out += c;
            }
        }
        return out;
    }

    Value parseNumber() {
        size_t start = pos_;
        if (peek() == '-') ++pos_;
        while (std::isdigit(static_cast<unsigned char>(peek()))) ++pos_;
        if (peek() == '.') {
            ++pos_;
            while (std::isdigit(static_cast<unsigned char>(peek()))) ++pos_;
        }
        if (peek() == 'e' || peek() == 'E') {
            ++pos_;
            if (peek() == '+' || peek() == '-') ++pos_;
            while (std::isdigit(static_cast<unsigned char>(peek()))) ++pos_;
        }
        if (pos_ == start) fail("invalid number");
        double d;
        try {
            d = std::stod(s_.substr(start, pos_ - start));
        } catch (...) {
            throw ParseError("invalid number at offset " + std::to_string(start));
        }
        if (!std::isfinite(d)) throw ParseError("non-finite number at offset " + std::to_string(start));
        return Value::makeNumber(d);
    }
};

inline Value parse(const std::string& text) { return Parser(text).parse(); }

// Serialize with fixed precision sufficient to round-trip doubles (17 sig digits).
inline void appendNumber(std::string& out, double d) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%.17g", d);
    out += buf;
}

inline void appendString(std::string& out, const std::string& s) {
    out += '"';
    for (char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default: out += c;
        }
    }
    out += '"';
}

inline void appendValue(std::string& out, const Value& v) {
    switch (v.type) {
        case Value::Type::Null: out += "null"; break;
        case Value::Type::Bool: out += v.boolean ? "true" : "false"; break;
        case Value::Type::Number: appendNumber(out, v.number); break;
        case Value::Type::String: appendString(out, v.str); break;
        case Value::Type::Array: {
            out += '[';
            for (size_t i = 0; i < v.arr.size(); ++i) {
                if (i) out += ',';
                appendValue(out, v.arr[i]);
            }
            out += ']';
            break;
        }
        case Value::Type::Object: {
            out += '{';
            bool first = true;
            for (const auto& kv : v.obj) {
                if (!first) out += ',';
                first = false;
                appendString(out, kv.first);
                out += ':';
                appendValue(out, kv.second);
            }
            out += '}';
            break;
        }
    }
}

inline std::string serialize(const Value& v) {
    std::string out;
    appendValue(out, v);
    return out;
}

}  // namespace json
