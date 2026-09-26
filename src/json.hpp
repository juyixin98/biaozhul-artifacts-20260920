// Minimal hand-written JSON parser / writer (no third-party dependencies).
// Sufficient for the dominance-fault-location service JSON interface.
#pragma once

#include <cstdint>
#include <string>
#include <vector>
#include <utility>
#include <stdexcept>
#include <sstream>
#include <cmath>

namespace json {

class Value {
public:
    enum Type { Null, Bool, Number, String, Array, Object } type = Null;

    bool boolean = false;
    bool isInt = false;
    long long integer = 0;
    double number = 0.0;
    std::string str;
    std::vector<Value> arr;
    std::vector<std::pair<std::string, Value>> obj;

    Value() = default;
    static Value makeBool(bool v) { Value x; x.type = Bool; x.boolean = v; return x; }
    static Value makeInt(long long v) { Value x; x.type = Number; x.isInt = true; x.integer = v; x.number = (double)v; return x; }
    static Value makeDouble(double v) { Value x; x.type = Number; x.isInt = false; x.number = v; return x; }
    static Value makeString(std::string v) { Value x; x.type = String; x.str = std::move(v); return x; }
    static Value makeArray() { Value x; x.type = Array; return x; }
    static Value makeObject() { Value x; x.type = Object; return x; }
    static Value makeNull() { Value x; x.type = Null; return x; }

    const Value* find(const std::string& key) const {
        if (type != Object) return nullptr;
        for (const auto& kv : obj) if (kv.first == key) return &kv.second;
        return nullptr;
    }
    Value& ensure(const std::string& key) {
        for (auto& kv : obj) if (kv.first == key) return kv.second;
        obj.emplace_back(key, Value());
        return obj.back().second;
    }
    void set(const std::string& key, Value v) { ensure(key) = std::move(v); }

    std::string dump(int indent = 0) const {
        std::string out;
        write(out, indent, 0);
        out.push_back('\n');
        return out;
    }

private:
    static void emitRaw(std::string& out, const std::string& s) {
        out.push_back('"');
        for (unsigned char c : s) {
            switch (c) {
                case '"': out += "\\\""; break;
                case '\\': out += "\\\\"; break;
                case '\n': out += "\\n"; break;
                case '\r': out += "\\r"; break;
                case '\t': out += "\\t"; break;
                case '\b': out += "\\b"; break;
                case '\f': out += "\\f"; break;
                default:
                    if (c < 0x20) {
                        char buf[8];
                        std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                        out += buf;
                    } else {
                        out.push_back((char)c); // pass UTF-8 through
                    }
            }
        }
        out.push_back('"');
    }

    static void emitNum(std::string& out, const Value& v) {
        if (v.isInt) {
            out += std::to_string(v.integer);
        } else if (std::isfinite(v.number)) {
            std::ostringstream oss;
            oss.precision(17);
            oss << v.number;
            out += oss.str();
        } else {
            out += "null"; // JSON has no NaN/Infinity
        }
    }

    void write(std::string& out, int indent, int depth) const {
        auto pad = [&](int d) { if (indent > 0) out.append((size_t)indent * d, ' '); };
        switch (type) {
            case Null: out += "null"; break;
            case Bool: out += boolean ? "true" : "false"; break;
            case Number: emitNum(out, *this); break;
            case String: emitRaw(out, str); break;
            case Array:
                if (arr.empty()) { out += "[]"; break; }
                out.push_back('[');
                for (size_t i = 0; i < arr.size(); ++i) {
                    if (i) out.push_back(',');
                    if (indent > 0) { out.push_back('\n'); pad(depth + 1); }
                    arr[i].write(out, indent, depth + 1);
                }
                if (indent > 0) { out.push_back('\n'); pad(depth); }
                out.push_back(']');
                break;
            case Object:
                if (obj.empty()) { out += "{}"; break; }
                out.push_back('{');
                for (size_t i = 0; i < obj.size(); ++i) {
                    if (i) out.push_back(',');
                    if (indent > 0) { out.push_back('\n'); pad(depth + 1); }
                    emitRaw(out, obj[i].first);
                    out += indent > 0 ? ": " : ":";
                    obj[i].second.write(out, indent, depth + 1);
                }
                if (indent > 0) { out.push_back('\n'); pad(depth); }
                out.push_back('}');
                break;
        }
    }
};

class Parser {
public:
    static Value parse(const std::string& text, std::string& err) {
        Parser p(text);
        p.skipWs();
        Value v = p.parseValue(0, err);
        if (!err.empty()) return Value();
        p.skipWs();
        if (p.pos != p.s.size()) { err = "trailing characters after JSON value"; return Value(); }
        return v;
    }

private:
    const std::string& s;
    size_t pos = 0;
    explicit Parser(const std::string& text) : s(text) {}

    [[noreturn]] void fail(const std::string& msg) { throw std::runtime_error(msg); }

    void skipWs() {
        while (pos < s.size()) {
            char c = s[pos];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos;
            else break;
        }
    }

    char peek() { return pos < s.size() ? s[pos] : '\0'; }

    Value parseValue(int depth, std::string& err) {
        if (depth > 128) { err = "JSON nesting too deep"; return Value(); }
        try {
            skipWs();
            if (pos >= s.size()) fail("unexpected end of input");
            switch (s[pos]) {
                case '{': return parseObject(depth, err);
                case '[': return parseArray(depth, err);
                case '"': return Value::makeString(parseString());
                case 't': case 'f': return parseBool();
                case 'n': return parseNull();
                default: return parseNumber();
            }
        } catch (const std::exception& e) {
            if (err.empty()) err = e.what();
            return Value();
        }
    }

    Value parseObject(int depth, std::string& err) {
        Value v = Value::makeObject();
        ++pos; // {
        skipWs();
        if (peek() == '}') { ++pos; return v; }
        while (true) {
            skipWs();
            if (peek() != '"') fail("expected string key in object");
            std::string key = parseString();
            skipWs();
            if (peek() != ':') fail("expected ':' after object key");
            ++pos;
            Value child = parseValue(depth + 1, err);
            if (!err.empty()) return Value();
            v.obj.emplace_back(std::move(key), std::move(child));
            skipWs();
            char c = peek();
            if (c == ',') { ++pos; continue; }
            if (c == '}') { ++pos; return v; }
            fail("expected ',' or '}' in object");
        }
    }

    Value parseArray(int depth, std::string& err) {
        Value v = Value::makeArray();
        ++pos; // [
        skipWs();
        if (peek() == ']') { ++pos; return v; }
        while (true) {
            Value child = parseValue(depth + 1, err);
            if (!err.empty()) return Value();
            v.arr.push_back(std::move(child));
            skipWs();
            char c = peek();
            if (c == ',') { ++pos; continue; }
            if (c == ']') { ++pos; return v; }
            fail("expected ',' or ']' in array");
        }
    }

    std::string parseString() {
        ++pos; // opening quote
        std::string out;
        while (pos < s.size()) {
            char c = s[pos++];
            if (c == '"') return out;
            if (c == '\\') {
                if (pos >= s.size()) fail("bad escape at end of input");
                char e = s[pos++];
                switch (e) {
                    case '"': out.push_back('"'); break;
                    case '\\': out.push_back('\\'); break;
                    case '/': out.push_back('/'); break;
                    case 'n': out.push_back('\n'); break;
                    case 'r': out.push_back('\r'); break;
                    case 't': out.push_back('\t'); break;
                    case 'b': out.push_back('\b'); break;
                    case 'f': out.push_back('\f'); break;
                    case 'u': {
                        unsigned hi = parseHex4();
                        unsigned code = hi;
                        if (hi >= 0xD800 && hi <= 0xDBFF) {
                            if (pos + 1 < s.size() && s[pos] == '\\' && s[pos + 1] == 'u') {
                                pos += 2;
                                unsigned lo = parseHex4();
                                if (lo >= 0xDC00 && lo <= 0xDFFF)
                                    code = 0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00);
                                else
                                    fail("bad low surrogate");
                            } else {
                                fail("expected low surrogate");
                            }
                        }
                        appendUtf8(out, code);
                        break;
                    }
                    default: fail("invalid escape character");
                }
            } else if ((unsigned char)c < 0x20) {
                fail("unescaped control character in string");
            } else {
                out.push_back(c);
            }
        }
        fail("unterminated string");
    }

    unsigned parseHex4() {
        if (pos + 4 > s.size()) fail("incomplete \\u escape");
        unsigned v = 0;
        for (int i = 0; i < 4; ++i) {
            char c = s[pos++];
            v <<= 4;
            if (c >= '0' && c <= '9') v |= (unsigned)(c - '0');
            else if (c >= 'a' && c <= 'f') v |= (unsigned)(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') v |= (unsigned)(c - 'A' + 10);
            else fail("invalid hex digit in \\u escape");
        }
        return v;
    }

    static void appendUtf8(std::string& out, unsigned cp) {
        if (cp < 0x80) {
            out.push_back((char)cp);
        } else if (cp < 0x800) {
            out.push_back((char)(0xC0 | (cp >> 6)));
            out.push_back((char)(0x80 | (cp & 0x3F)));
        } else if (cp < 0x10000) {
            out.push_back((char)(0xE0 | (cp >> 12)));
            out.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back((char)(0x80 | (cp & 0x3F)));
        } else {
            out.push_back((char)(0xF0 | (cp >> 18)));
            out.push_back((char)(0x80 | ((cp >> 12) & 0x3F)));
            out.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back((char)(0x80 | (cp & 0x3F)));
        }
    }

    Value parseBool() {
        if (s.compare(pos, 4, "true") == 0) { pos += 4; return Value::makeBool(true); }
        if (s.compare(pos, 5, "false") == 0) { pos += 5; return Value::makeBool(false); }
        fail("invalid literal");
    }

    Value parseNull() {
        if (s.compare(pos, 4, "null") == 0) { pos += 4; return Value::makeNull(); }
        fail("invalid literal");
    }

    Value parseNumber() {
        size_t start = pos;
        bool integral = true;
        if (peek() == '-') ++pos;
        if (peek() == '0') ++pos;
        else if (peek() >= '1' && peek() <= '9') { while (peek() >= '0' && peek() <= '9') ++pos; }
        else fail("invalid number");
        if (peek() == '.') {
            integral = false;
            ++pos;
            if (!(peek() >= '0' && peek() <= '9')) fail("invalid number: expected fraction digits");
            while (peek() >= '0' && peek() <= '9') ++pos;
        }
        if (peek() == 'e' || peek() == 'E') {
            integral = false;
            ++pos;
            if (peek() == '+' || peek() == '-') ++pos;
            if (!(peek() >= '0' && peek() <= '9')) fail("invalid number: expected exponent digits");
            while (peek() >= '0' && peek() <= '9') ++pos;
        }
        std::string token = s.substr(start, pos - start);
        Value v;
        v.type = Value::Number;
        if (integral) {
            try {
                v.integer = std::stoll(token);
                v.isInt = true;
                v.number = (double)v.integer;
                return v;
            } catch (...) {
                // fall through to double
            }
        }
        v.isInt = false;
        v.number = std::stod(token);
        return v;
    }
};

inline Value parse(const std::string& text, std::string& err) {
    return Parser::parse(text, err);
}

} // namespace json
