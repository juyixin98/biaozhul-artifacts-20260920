#include "json.h"

#include <cmath>
#include <cstdio>
#include <stdexcept>

namespace json {

namespace {

struct Parser {
    const std::string& s;
    std::size_t pos = 0;
    std::string err;

    explicit Parser(const std::string& text) : s(text) {}

    [[noreturn]] void fail(const std::string& msg) {
        char where[64];
        std::snprintf(where, sizeof(where), " at offset %zu", pos);
        throw std::runtime_error(msg + where);
    }

    void skipWs() {
        while (pos < s.size()) {
            char c = s[pos];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                ++pos;
            } else {
                break;
            }
        }
    }

    char peek() {
        if (pos >= s.size()) fail("unexpected end of input");
        return s[pos];
    }

    void expect(char c) {
        if (pos >= s.size() || s[pos] != c) fail(std::string("expected '") + c + "'");
        ++pos;
    }

    Value parseValue() {
        skipWs();
        char c = peek();
        switch (c) {
            case '{': return parseObject();
            case '[': return parseArray();
            case '"': {
                Value v;
                v.type = Type::String;
                v.str = parseString();
                return v;
            }
            case 't':
            case 'f': return parseBool();
            case 'n': return parseNull();
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
                fail("unexpected character");
        }
    }

    Value parseObject() {
        Value v;
        v.type = Type::Object;
        expect('{');
        skipWs();
        if (peek() == '}') { ++pos; return v; }
        while (true) {
            skipWs();
            if (peek() != '"') fail("expected string key");
            std::string key = parseString();
            skipWs();
            expect(':');
            Value val = parseValue();
            auto it = v.objIndex.find(key);
            if (it == v.objIndex.end()) {
                v.objIndex[key] = v.obj.size();
                v.obj.emplace_back(std::move(key), std::move(val));
            } else {
                // 重复键：更新查找索引指向的槽位值。
                v.obj[it->second].second = std::move(val);
            }
            skipWs();
            char c = peek();
            if (c == ',') { ++pos; continue; }
            if (c == '}') { ++pos; break; }
            fail("expected ',' or '}'");
        }
        return v;
    }

    Value parseArray() {
        Value v;
        v.type = Type::Array;
        expect('[');
        skipWs();
        if (peek() == ']') { ++pos; return v; }
        while (true) {
            v.arr.push_back(parseValue());
            skipWs();
            char c = peek();
            if (c == ',') { ++pos; continue; }
            if (c == ']') { ++pos; break; }
            fail("expected ',' or ']'");
        }
        return v;
    }

    std::string parseString() {
        expect('"');
        std::string out;
        while (true) {
            if (pos >= s.size()) fail("unterminated string");
            char c = s[pos++];
            if (c == '"') break;
            if (static_cast<unsigned char>(c) < 0x20) fail("unescaped control char");
            if (c != '\\') {
                out.push_back(c); // 原样保留（含 UTF-8 多字节）
                continue;
            }
            if (pos >= s.size()) fail("bad escape");
            char e = s[pos++];
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
                // UTF-16 代理对
                if (code >= 0xD800 && code <= 0xDBFF) {
                    if (pos + 1 < s.size() && s[pos] == '\\' && s[pos + 1] == 'u') {
                        pos += 2;
                        unsigned lo = parseHex4();
                        if (lo >= 0xDC00 && lo <= 0xDFFF) {
                            code = 0x10000 + ((code - 0xD800) << 10) + (lo - 0xDC00);
                        } else {
                            fail("bad low surrogate");
                        }
                    } else {
                        fail("expected low surrogate");
                    }
                }
                appendUtf8(out, code);
                break;
            }
                default: fail("bad escape character");
            }
        }
        return out;
    }

    unsigned parseHex4() {
        if (pos + 4 > s.size()) fail("bad \\u escape");
        unsigned code = 0;
        for (int i = 0; i < 4; ++i) {
            char c = s[pos++];
            code <<= 4;
            if (c >= '0' && c <= '9') code += static_cast<unsigned>(c - '0');
            else if (c >= 'a' && c <= 'f') code += static_cast<unsigned>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') code += static_cast<unsigned>(c - 'A' + 10);
            else fail("bad hex digit");
        }
        return code;
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

    Value parseBool() {
        if (s.compare(pos, 4, "true") == 0) {
            pos += 4;
            Value v; v.type = Type::Bool; v.boolean = true; return v;
        }
        if (s.compare(pos, 5, "false") == 0) {
            pos += 5;
            Value v; v.type = Type::Bool; v.boolean = false; return v;
        }
        fail("invalid literal");
    }

    Value parseNull() {
        if (s.compare(pos, 4, "null") == 0) {
            pos += 4;
            return Value{};
        }
        fail("invalid literal");
    }

    Value parseNumber() {
        std::size_t start = pos;
        if (peek() == '-') ++pos;
        if (pos >= s.size()) fail("bad number");
        if (s[pos] == '0') {
            ++pos;
        } else if (s[pos] >= '1' && s[pos] <= '9') {
            while (pos < s.size() && s[pos] >= '0' && s[pos] <= '9') ++pos;
        } else {
            fail("bad number");
        }
        if (pos < s.size() && s[pos] == '.') {
            ++pos;
            if (pos >= s.size() || s[pos] < '0' || s[pos] > '9') fail("bad fraction");
            while (pos < s.size() && s[pos] >= '0' && s[pos] <= '9') ++pos;
        }
        if (pos < s.size() && (s[pos] == 'e' || s[pos] == 'E')) {
            ++pos;
            if (pos < s.size() && (s[pos] == '+' || s[pos] == '-')) ++pos;
            if (pos >= s.size() || s[pos] < '0' || s[pos] > '9') fail("bad exponent");
            while (pos < s.size() && s[pos] >= '0' && s[pos] <= '9') ++pos;
        }
        std::string token = s.substr(start, pos - start);
        long double val = std::strtold(token.c_str(), nullptr);
        if (!std::isfinite(val)) fail("number out of range");
        Value v;
        v.type = Type::Number;
        v.number = val;
        return v;
    }
};

} // namespace

const Value* Value::find(const std::string& key) const {
    auto it = objIndex.find(key);
    if (it == objIndex.end()) return nullptr;
    return &obj[it->second].second;
}

std::unique_ptr<Value> parse(const std::string& text, std::string* error) {
    Parser parser(text);
    try {
        Value root = parser.parseValue();
        parser.skipWs();
        if (parser.pos != text.size()) parser.fail("trailing characters");
        return std::make_unique<Value>(std::move(root));
    } catch (const std::runtime_error& e) {
        if (error) *error = e.what();
        return nullptr;
    }
}

} // namespace json
