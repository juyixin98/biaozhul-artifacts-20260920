// SPDX-License-Identifier: MIT
#include "segi/json.hpp"

#include <cmath>
#include <cstdio>
#include <sstream>

namespace segi {

namespace {

struct Parser {
    const std::string& s;
    size_t i = 0;
    explicit Parser(const std::string& src) : s(src) {}

    [[noreturn]] void fail(const std::string& m) { throw JsonError(m, i); }

    void ws() {
        while (i < s.size()) {
            char c = s[i];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++i;
            else break;
        }
    }
    char peek() { if (i >= s.size()) fail("unexpected end of input"); return s[i]; }
    bool eat(char c) { ws(); if (i < s.size() && s[i] == c) { ++i; return true; } return false; }
    void expect(char c) {
        ws();
        if (i >= s.size() || s[i] != c) fail(std::string("expected '") + c + "'");
        ++i;
    }

    JVal parseValue() {
        ws();
        if (i >= s.size()) fail("expected value");
        char c = s[i];
        if (c == '{') return parseObject();
        if (c == '[') return parseArray();
        if (c == '"') return JVal{JType::String, false, "", parseString(), {}, {}};
        if (c == 't' || c == 'f') return parseBool();
        if (c == 'n') return parseNull();
        if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
        fail("unexpected character");
    }

    JVal parseObject() {
        JVal v; v.type = JType::Object;
        expect('{');
        ws();
        if (eat('}')) return v;
        while (true) {
            ws();
            if (peek() != '"') fail("expected string key");
            std::string key = parseString();
            expect(':');
            v.obj.emplace_back(std::move(key), parseValue());
            ws();
            if (eat(',')) continue;
            if (eat('}')) break;
            fail("expected ',' or '}'");
        }
        return v;
    }

    JVal parseArray() {
        JVal v; v.type = JType::Array;
        expect('[');
        ws();
        if (eat(']')) return v;
        while (true) {
            v.arr.push_back(parseValue());
            ws();
            if (eat(',')) continue;
            if (eat(']')) break;
            fail("expected ',' or ']'");
        }
        return v;
    }

    std::string parseString() {
        expect('"');
        std::string out;
        while (true) {
            if (i >= s.size()) fail("unterminated string");
            char c = s[i++];
            if (c == '"') break;
            if (c == '\\') {
                if (i >= s.size()) fail("bad escape");
                char e = s[i++];
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
                        if (i + 4 > s.size()) fail("bad unicode escape");
                        unsigned cp = 0;
                        for (int k = 0; k < 4; ++k) {
                            char h = s[i++];
                            cp <<= 4;
                            if (h >= '0' && h <= '9') cp += h - '0';
                            else if (h >= 'a' && h <= 'f') cp += h - 'a' + 10;
                            else if (h >= 'A' && h <= 'F') cp += h - 'A' + 10;
                            else fail("bad hex digit");
                        }
                        // UTF-8 编码（不处理代理对组合的完整校验，常见输入足够；非法代理按字面值发出）
                        if (cp < 0x80) out.push_back(char(cp));
                        else if (cp < 0x800) {
                            out.push_back(char(0xC0 | (cp >> 6)));
                            out.push_back(char(0x80 | (cp & 0x3F)));
                        } else {
                            out.push_back(char(0xE0 | (cp >> 12)));
                            out.push_back(char(0x80 | ((cp >> 6) & 0x3F)));
                            out.push_back(char(0x80 | (cp & 0x3F)));
                        }
                        break;
                    }
                    default: fail("bad escape char");
                }
            } else if ((unsigned char)c < 0x20) {
                fail("control character in string");
            } else {
                out.push_back(c);
            }
        }
        return out;
    }

    JVal parseBool() {
        if (s.compare(i, 4, "true") == 0) { i += 4; return JVal{JType::Bool, true, "", "", {}, {}}; }
        if (s.compare(i, 5, "false") == 0) { i += 5; return JVal{JType::Bool, false, "", "", {}, {}}; }
        fail("invalid literal");
    }

    JVal parseNull() {
        if (s.compare(i, 4, "null") == 0) { i += 4; return JVal{JType::Null, false, "", "", {}, {}}; }
        fail("invalid literal");
    }

    JVal parseNumber() {
        size_t start = i;
        if (s[i] == '-') ++i;
        if (i >= s.size()) fail("bad number");
        if (s[i] == '0') ++i;
        else if (s[i] >= '1' && s[i] <= '9') { while (i < s.size() && s[i] >= '0' && s[i] <= '9') ++i; }
        else fail("bad number");
        bool isFrac = false;
        if (i < s.size() && s[i] == '.') {
            isFrac = true;
            ++i;
            if (i >= s.size() || s[i] < '0' || s[i] > '9') fail("bad fraction");
            while (i < s.size() && s[i] >= '0' && s[i] <= '9') ++i;
        }
        if (i < s.size() && (s[i] == 'e' || s[i] == 'E')) {
            isFrac = true;
            ++i;
            if (i < s.size() && (s[i] == '+' || s[i] == '-')) ++i;
            if (i >= s.size() || s[i] < '0' || s[i] > '9') fail("bad exponent");
            while (i < s.size() && s[i] >= '0' && s[i] <= '9') ++i;
        }
        (void)isFrac; // 上层据此拒绝非整数（见 main.cpp 校验）
        JVal v;
        v.type = JType::Number;
        v.raw = s.substr(start, i - start);
        return v;
    }
};

void dumpString(const std::string& s, std::string& out) {
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
                } else out.push_back(char(c));
        }
    }
    out.push_back('"');
}

void dumpInto(const JVal& v, std::string& out, bool pretty, int depth) {
    auto nl = [&]() { if (pretty) { out.push_back('\n'); out.append(2 * (depth + 1), ' '); } };
    auto sep = [&]() { if (pretty) out.push_back(' '); };
    switch (v.type) {
        case JType::Null: out += "null"; break;
        case JType::Bool: out += v.boolean ? "true" : "false"; break;
        case JType::Number: out += v.raw.empty() ? "0" : v.raw; break;
        case JType::String: dumpString(v.str, out); break;
        case JType::Array: {
            if (v.arr.empty()) { out += "[]"; break; }
            out.push_back('[');
            for (size_t k = 0; k < v.arr.size(); ++k) {
                if (k) out.push_back(',');
                nl();
                dumpInto(v.arr[k], out, pretty, depth + 1);
            }
            if (pretty) { out.push_back('\n'); out.append(2 * depth, ' '); }
            out.push_back(']');
            break;
        }
        case JType::Object: {
            if (v.obj.empty()) { out += "{}"; break; }
            out.push_back('{');
            for (size_t k = 0; k < v.obj.size(); ++k) {
                if (k) out.push_back(',');
                nl();
                dumpString(v.obj[k].first, out);
                out.push_back(':');
                sep();
                dumpInto(v.obj[k].second, out, pretty, depth + 1);
            }
            if (pretty) { out.push_back('\n'); out.append(2 * depth, ' '); }
            out.push_back('}');
            break;
        }
    }
}

} // namespace

JVal jsonParse(const std::string& text) {
    Parser p(text);
    JVal v = p.parseValue();
    p.ws();
    if (p.i != text.size()) throw JsonError("trailing data after JSON value", p.i);
    return v;
}

std::string jsonDump(const JVal& v, bool pretty, unsigned indent) {
    (void)indent;
    std::string out;
    dumpInto(v, out, pretty, 0);
    return out;
}

JVal jNum(const std::string& rawDecimal) { JVal v; v.type = JType::Number; v.raw = rawDecimal; return v; }
JVal jStr(std::string s) { JVal v; v.type = JType::String; v.str = std::move(s); return v; }
JVal jArr() { JVal v; v.type = JType::Array; return v; }
JVal jObj() { JVal v; v.type = JType::Object; return v; }
void jPush(JVal& arr, JVal v) { arr.arr.push_back(std::move(v)); }
void jSet(JVal& obj, const std::string& key, JVal v) {
    for (auto& kv : obj.obj) if (kv.first == key) { kv.second = std::move(v); return; }
    obj.obj.emplace_back(key, std::move(v));
}

} // namespace segi
