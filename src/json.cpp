// json.cpp — 递归下降 JSON 解析器
#include "json.hpp"

#include <cmath>
#include <cstdio>
#include <sstream>

namespace json {

namespace {

class Parser {
public:
    explicit Parser(const std::string& s) : s_(s) {}

    ParseResult run() {
        ParseResult r;
        skipWs();
        try {
            r.value = parseValue();
        } catch (const std::string& e) {
            r.ok = false;
            r.error = e;
            return r;
        }
        skipWs();
        if (pos_ != s_.size()) {
            r.ok = false;
            r.error = "trailing characters at offset " + std::to_string(pos_);
            return r;
        }
        r.ok = true;
        return r;
    }

private:
    const std::string& s_;
    size_t pos_ = 0;

    [[noreturn]] void fail(const std::string& msg) {
        throw msg + " at offset " + std::to_string(pos_);
    }

    char peek() const { return pos_ < s_.size() ? s_[pos_] : '\0'; }
    char get() { return pos_ < s_.size() ? s_[pos_++] : '\0'; }
    void expect(char c) {
        if (get() != c) fail(std::string("expected '") + c + "'");
    }

    void skipWs() {
        while (pos_ < s_.size()) {
            char c = s_[pos_];
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
            case '"': return Value::makeString(parseString());
            case 't': case 'f': return parseBool();
            case 'n': return parseNull();
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
                fail("unexpected character");
        }
    }

    Value parseObject() {
        expect('{');
        Value v = Value::makeObject();
        skipWs();
        if (peek() == '}') { get(); return v; }
        while (true) {
            skipWs();
            if (peek() != '"') fail("expected string key");
            std::string key = parseString();
            skipWs();
            expect(':');
            v.obj[key] = parseValue();
            skipWs();
            char c = get();
            if (c == ',') continue;
            if (c == '}') break;
            fail("expected ',' or '}'");
        }
        return v;
    }

    Value parseArray() {
        expect('[');
        Value v = Value::makeArray();
        skipWs();
        if (peek() == ']') { get(); return v; }
        while (true) {
            v.arr.push_back(parseValue());
            skipWs();
            char c = get();
            if (c == ',') continue;
            if (c == ']') break;
            fail("expected ',' or ']'");
        }
        return v;
    }

    std::string parseString() {
        expect('"');
        std::string out;
        while (true) {
            if (pos_ >= s_.size()) fail("unterminated string");
            char c = get();
            if (c == '"') break;
            if (c == '\\') {
                if (pos_ >= s_.size()) fail("unterminated escape");
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
                        unsigned code = parseHex4();
                        // 处理 UTF-16 代理对
                        if (code >= 0xD800 && code <= 0xDBFF) {
                            if (get() != '\\' || get() != 'u')
                                fail("expected low surrogate");
                            unsigned lo = parseHex4();
                            if (lo < 0xDC00 || lo > 0xDFFF) fail("bad low surrogate");
                            code = 0x10000 + ((code - 0xD800) << 10) + (lo - 0xDC00);
                        }
                        appendUtf8(out, code);
                        break;
                    }
                    default: fail("invalid escape");
                }
            } else if (static_cast<unsigned char>(c) < 0x20) {
                fail("unescaped control character in string");
            } else {
                out.push_back(c);  // 原样保留 UTF-8 字节
            }
        }
        return out;
    }

    unsigned parseHex4() {
        unsigned v = 0;
        for (int i = 0; i < 4; ++i) {
            if (pos_ >= s_.size()) fail("bad \\u escape");
            char c = get();
            v <<= 4;
            if (c >= '0' && c <= '9') v |= c - '0';
            else if (c >= 'a' && c <= 'f') v |= c - 'a' + 10;
            else if (c >= 'A' && c <= 'F') v |= c - 'A' + 10;
            else fail("bad hex digit");
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

    Value parseNumber() {
        size_t start = pos_;
        if (peek() == '-') get();
        if (peek() == '0') get();
        else if (peek() >= '1' && peek() <= '9') { while (peek() >= '0' && peek() <= '9') get(); }
        else fail("invalid number");
        if (peek() == '.') {
            get();
            if (!(peek() >= '0' && peek() <= '9')) fail("expected fraction digit");
            while (peek() >= '0' && peek() <= '9') get();
        }
        if (peek() == 'e' || peek() == 'E') {
            get();
            if (peek() == '+' || peek() == '-') get();
            if (!(peek() >= '0' && peek() <= '9')) fail("expected exponent digit");
            while (peek() >= '0' && peek() <= '9') get();
        }
        std::string token = s_.substr(start, pos_ - start);
        Value v;
        v.type = Type::Number;
        v.number = std::strtod(token.c_str(), nullptr);
        return v;
    }

    Value parseBool() {
        if (s_.compare(pos_, 4, "true") == 0) { pos_ += 4; return Value::makeBool(true); }
        if (s_.compare(pos_, 5, "false") == 0) { pos_ += 5; return Value::makeBool(false); }
        fail("invalid literal");
    }

    Value parseNull() {
        if (s_.compare(pos_, 4, "null") == 0) { pos_ += 4; return Value(); }
        fail("invalid literal");
    }
};

void emitString(std::string& out, const std::string& s) {
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
                    out.push_back(static_cast<char>(c));  // UTF-8 原样
                }
        }
    }
    out.push_back('"');
}

void emitNumber(std::string& out, double d) {
    if (!std::isfinite(d)) {
        // JSON 不允许 NaN/Inf；正常流程不会产生，这里保底输出 null
        out += "null";
        return;
    }
    char buf[40];
    // %.17g 保证 double 往返不丢精度；整数也会形如 1（%g 去掉无意义的 0）
    std::snprintf(buf, sizeof(buf), "%.17g", d);
    out += buf;
}

void emit(std::string& out, const Value& v, int indent, int depth) {
    auto pad = [&](int n) {
        if (indent >= 0) out.append(static_cast<size_t>(indent) * n, ' ');
    };
    switch (v.type) {
        case Type::Null: out += "null"; break;
        case Type::Bool: out += v.boolean ? "true" : "false"; break;
        case Type::Number: emitNumber(out, v.number); break;
        case Type::String: emitString(out, v.str); break;
        case Type::Array: {
            if (v.arr.empty()) { out += "[]"; break; }
            out.push_back('[');
            if (indent >= 0) out.push_back('\n');
            for (size_t i = 0; i < v.arr.size(); ++i) {
                pad(depth + 1);
                emit(out, v.arr[i], indent, depth + 1);
                if (i + 1 < v.arr.size()) out.push_back(',');
                if (indent >= 0) out.push_back('\n');
            }
            pad(depth);
            out.push_back(']');
            break;
        }
        case Type::Object: {
            if (v.obj.empty()) { out += "{}"; break; }
            out.push_back('{');
            if (indent >= 0) out.push_back('\n');
            size_t i = 0;
            for (const auto& [k, val] : v.obj) {
                pad(depth + 1);
                emitString(out, k);
                out += indent >= 0 ? ": " : ":";
                emit(out, val, indent, depth + 1);
                if (++i < v.obj.size()) out.push_back(',');
                if (indent >= 0) out.push_back('\n');
            }
            pad(depth);
            out.push_back('}');
            break;
        }
    }
}

}  // namespace

ParseResult parse(const std::string& text) { return Parser(text).run(); }

std::string dump(const Value& v, int indent) {
    std::string out;
    emit(out, v, indent, 0);
    return out;
}

}  // namespace json
