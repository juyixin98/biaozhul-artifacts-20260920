// Minimal JSON parser / serializer (no external dependencies).
// Supports null, bool, number (double), string, array, ordered object.
#pragma once

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace json {

struct Value {
    enum Type { Null, Bool, Num, Str, Arr, Obj } type = Null;
    bool b = false;
    double n = 0.0;
    std::string s;
    std::vector<Value> a;
    std::vector<std::pair<std::string, Value>> o; // insertion-ordered object

    // Convenience constructors
    Value() = default;
    static Value makeNum(double x) { Value v; v.type = Num; v.n = x; return v; }
    static Value makeBool(bool x) { Value v; v.type = Bool; v.b = x; return v; }
    static Value makeStr(std::string x) { Value v; v.type = Str; v.s = std::move(x); return v; }
    static Value makeArr() { Value v; v.type = Arr; return v; }
    static Value makeObj() { Value v; v.type = Obj; return v; }

    const Value* find(const std::string& key) const {
        if (type != Obj) return nullptr;
        for (const auto& kv : o) if (kv.first == key) return &kv.second;
        return nullptr;
    }
    Value& set(const std::string& key, Value val) {
        for (auto& kv : o) if (kv.first == key) { kv.second = std::move(val); return kv.second; }
        o.emplace_back(key, std::move(val));
        return o.back().second;
    }
    void push(Value val) { a.emplace_back(std::move(val)); }
};

class ParseError : public std::runtime_error {
public:
    size_t offset;
    ParseError(const std::string& msg, size_t off) : std::runtime_error(msg), offset(off) {}
};

class Parser {
public:
    explicit Parser(const std::string& src) : src_(src) {}

    Value parse() {
        skipWs();
        Value v = parseValue();
        skipWs();
        if (pos_ != src_.size()) fail("trailing characters after JSON value");
        return v;
    }

private:
    const std::string& src_;
    size_t pos_ = 0;

    [[noreturn]] void fail(const std::string& msg) { throw ParseError(msg, pos_); }

    char peek() const { return pos_ < src_.size() ? src_[pos_] : '\0'; }
    char getc() { return pos_ < src_.size() ? src_[pos_++] : '\0'; }
    void expect(char c) {
        if (peek() != c) fail(std::string("expected '") + c + "'");
        ++pos_;
    }

    void skipWs() {
        while (pos_ < src_.size()) {
            char c = src_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos_;
            else break;
        }
    }

    Value parseValue() {
        skipWs();
        char c = peek();
        switch (c) {
            case '{': return parseObject();
            case '[': return parseArray();
            case '"': return Value::makeStr(parseString());
            case 't': case 'f': return parseBool();
            case 'N': // lenient: NaN (Python/JS style); rejected by point validator
                if (src_.compare(pos_, 3, "NaN") == 0) { pos_ += 3; return Value::makeNum(std::nan("")); }
                fail("invalid literal");
            case 'n': return parseNull();
            default:
                if (c == 'I') return parseInfinity(1.0);
                if (c == '-' && src_.compare(pos_ + 1, 8, "Infinity") == 0)
                    return parseInfinity(-1.0);
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
                fail("unexpected character");
        }
    }

    Value parseInfinity(double sign) {
        if (src_.compare(pos_, 8, "Infinity") != 0) fail("invalid literal");
        pos_ += 8;
        return Value::makeNum(sign * std::numeric_limits<double>::infinity());
    }

    Value parseObject() {
        Value v = Value::makeObj();
        expect('{');
        skipWs();
        if (peek() == '}') { ++pos_; return v; }
        while (true) {
            skipWs();
            if (peek() != '"') fail("expected string key in object");
            std::string key = parseString();
            skipWs();
            expect(':');
            Value val = parseValue();
            v.o.emplace_back(std::move(key), std::move(val));
            skipWs();
            char c = getc();
            if (c == ',') continue;
            if (c == '}') break;
            fail("expected ',' or '}' in object");
        }
        return v;
    }

    Value parseArray() {
        Value v = Value::makeArr();
        expect('[');
        skipWs();
        if (peek() == ']') { ++pos_; return v; }
        while (true) {
            v.a.push_back(parseValue());
            skipWs();
            char c = getc();
            if (c == ',') continue;
            if (c == ']') break;
            fail("expected ',' or ']' in array");
        }
        return v;
    }

    Value parseBool() {
        if (src_.compare(pos_, 4, "true") == 0) { pos_ += 4; return Value::makeBool(true); }
        if (src_.compare(pos_, 5, "false") == 0) { pos_ += 5; return Value::makeBool(false); }
        fail("invalid literal");
    }

    Value parseNull() {
        if (src_.compare(pos_, 4, "null") == 0) { pos_ += 4; return Value(); }
        fail("invalid literal");
    }

    Value parseNumber() {
        size_t start = pos_;
        if (peek() == '-') ++pos_;
        while (peek() >= '0' && peek() <= '9') ++pos_;
        if (peek() == '.') {
            ++pos_;
            while (peek() >= '0' && peek() <= '9') ++pos_;
        }
        if (peek() == 'e' || peek() == 'E') {
            ++pos_;
            if (peek() == '+' || peek() == '-') ++pos_;
            while (peek() >= '0' && peek() <= '9') ++pos_;
        }
        std::string tok = src_.substr(start, pos_ - start);
        try {
            size_t used = 0;
            double d = std::stod(tok, &used);
            if (used != tok.size()) fail("invalid number");
            return Value::makeNum(d);
        } catch (...) {
            fail("invalid number");
        }
    }

    std::string parseString() {
        expect('"');
        std::string out;
        while (true) {
            char c = getc();
            if (c == '\0') fail("unterminated string");
            if (c == '"') break;
            if (c == '\\') {
                char e = getc();
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
                            char h = getc();
                            code <<= 4;
                            if (h >= '0' && h <= '9') code |= h - '0';
                            else if (h >= 'a' && h <= 'f') code |= h - 'a' + 10;
                            else if (h >= 'A' && h <= 'F') code |= h - 'A' + 10;
                            else fail("invalid \\u escape");
                        }
                        appendUtf8(out, code);
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
};

inline Value parse(const std::string& text) { return Parser(text).parse(); }

namespace detail {

inline void dumpString(std::string& out, const std::string& s) {
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

inline void dumpNum(std::string& out, double d) {
    if (std::isnan(d) || std::isinf(d)) { out += "null"; return; }
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%.17g", d);
    out += buf;
}

inline void dumpInto(std::string& out, const Value& v, int indent, int depth) {
    auto nl = [&](int extra = 0) {
        if (indent >= 0) {
            out.push_back('\n');
            out.append(static_cast<size_t>(indent) * (depth + extra), ' ');
        }
    };
    switch (v.type) {
        case Value::Null: out += "null"; break;
        case Value::Bool: out += v.b ? "true" : "false"; break;
        case Value::Num: dumpNum(out, v.n); break;
        case Value::Str: dumpString(out, v.s); break;
        case Value::Arr:
            if (v.a.empty()) { out += "[]"; break; }
            out.push_back('[');
            for (size_t i = 0; i < v.a.size(); ++i) {
                if (i) out.push_back(',');
                nl(1);
                dumpInto(out, v.a[i], indent, depth + 1);
            }
            nl();
            out.push_back(']');
            break;
        case Value::Obj:
            if (v.o.empty()) { out += "{}"; break; }
            out.push_back('{');
            for (size_t i = 0; i < v.o.size(); ++i) {
                if (i) out.push_back(',');
                nl(1);
                dumpString(out, v.o[i].first);
                out += indent >= 0 ? ": " : ":";
                dumpInto(out, v.o[i].second, indent, depth + 1);
            }
            nl();
            out.push_back('}');
            break;
    }
}

} // namespace detail

// indent < 0 produces compact output.
inline std::string dump(const Value& v, int indent = 2) {
    std::string out;
    detail::dumpInto(out, v, indent, 0);
    return out;
}

} // namespace json
