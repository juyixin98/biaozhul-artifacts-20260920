#include "json.hpp"

#include <cmath>
#include <cstdio>
#include <cctype>
#include <limits>
#include <stdexcept>

namespace json {

const Value& Value::get(const std::string& key) const {
    auto it = members.find(key);
    if (it == members.end()) {
        throw std::out_of_range("json key not found: " + key);
    }
    return it->second;
}

Value& Value::set(const std::string& key, Value v) {
    type = Type::Object;
    auto it = members.find(key);
    if (it == members.end()) {
        keys.push_back(key);
    }
    auto result = members.emplace(key, std::move(v));
    if (!result.second) {
        result.first->second = std::move(v);
    }
    return result.first->second;
}

namespace {

class Parser {
public:
    explicit Parser(const std::string& text) : s(text), pos(0), line(1), col(1) {}

    Value parseDocument() {
        skipWs();
        Value v = parseValue();
        skipWs();
        if (pos != s.size()) {
            fail("trailing characters after JSON value");
        }
        return v;
    }

private:
    const std::string& s;
    size_t pos;
    int line;
    int col;

    [[noreturn]] void fail(const std::string& msg) {
        throw ParseError{msg, line, col};
    }

    char peek() const { return pos < s.size() ? s[pos] : '\0'; }

    char advance() {
        char c = s[pos++];
        if (c == '\n') { ++line; col = 1; } else { ++col; }
        return c;
    }

    bool consume(char c) {
        if (peek() == c) { advance(); return true; }
        return false;
    }

    void expect(char c, const char* what) {
        if (!consume(c)) fail(std::string("expected ") + what);
    }

    void skipWs() {
        while (pos < s.size()) {
            char c = s[pos];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                advance();
            } else {
                break;
            }
        }
    }

    Value parseValue() {
        skipWs();
        char c = peek();
        switch (c) {
            case '{': return parseObject();
            case '[': return parseArray();
            case '"': return Value(parseString());
            case 't': case 'f': return parseBool();
            case 'n': return parseNull();
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
                if (pos == s.size()) fail("unexpected end of input");
                fail(std::string("unexpected character '") + c + "'");
        }
    }

    Value parseObject() {
        Value v = Value::object();
        expect('{', "'{'");
        skipWs();
        if (consume('}')) return v;
        while (true) {
            skipWs();
            if (peek() != '"') fail("expected string key in object");
            std::string key = parseString();
            skipWs();
            expect(':', "':'");
            Value member = parseValue();
            v.set(key, std::move(member));
            skipWs();
            if (consume(',')) continue;
            expect('}', "'}' or ','");
            break;
        }
        return v;
    }

    Value parseArray() {
        Value v = Value::array();
        expect('[', "'['");
        skipWs();
        if (consume(']')) return v;
        while (true) {
            v.push(parseValue());
            skipWs();
            if (consume(',')) continue;
            expect(']', "']' or ','");
            break;
        }
        return v;
    }

    Value parseBool() {
        if (s.compare(pos, 4, "true") == 0) {
            for (int i = 0; i < 4; ++i) advance();
            return Value(true);
        }
        if (s.compare(pos, 5, "false") == 0) {
            for (int i = 0; i < 5; ++i) advance();
            return Value(false);
        }
        fail("invalid literal");
    }

    Value parseNull() {
        if (s.compare(pos, 4, "null") == 0) {
            for (int i = 0; i < 4; ++i) advance();
            return Value();
        }
        fail("invalid literal");
    }

    Value parseNumber() {
        size_t start = pos;
        consume('-');
        if (peek() == '0') {
            advance();
        } else if (peek() >= '1' && peek() <= '9') {
            while (std::isdigit(static_cast<unsigned char>(peek()))) advance();
        } else {
            fail("invalid number");
        }
        if (consume('.')) {
            if (!std::isdigit(static_cast<unsigned char>(peek()))) fail("digits expected after '.'");
            while (std::isdigit(static_cast<unsigned char>(peek()))) advance();
        }
        if (peek() == 'e' || peek() == 'E') {
            advance();
            consume('+') || consume('-');
            if (!std::isdigit(static_cast<unsigned char>(peek()))) fail("digits expected in exponent");
            while (std::isdigit(static_cast<unsigned char>(peek()))) advance();
        }
        const std::string token = s.substr(start, pos - start);
        try {
            size_t used = 0;
            double d = std::stod(token, &used);
            return Value(d);
        } catch (...) {
            fail("number out of range: " + token);
        }
    }

    void appendUtf8(std::string& out, uint32_t cp) {
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

    uint32_t parseHex4() {
        uint32_t v = 0;
        for (int i = 0; i < 4; ++i) {
            char c = peek();
            uint32_t d;
            if (c >= '0' && c <= '9') d = static_cast<uint32_t>(c - '0');
            else if (c >= 'a' && c <= 'f') d = static_cast<uint32_t>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') d = static_cast<uint32_t>(c - 'A' + 10);
            else fail("invalid unicode escape");
            advance();
            v = (v << 4) | d;
        }
        return v;
    }

    std::string parseString() {
        expect('"', "'\"'");
        std::string out;
        while (true) {
            if (pos == s.size()) fail("unterminated string");
            char c = advance();
            if (c == '"') break;
            if (c == '\n' || c == '\r') fail("raw newline in string");
            if (c != '\\') {
                out.push_back(c);
                continue;
            }
            if (pos == s.size()) fail("unterminated escape");
            char e = advance();
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
                    uint32_t cp = parseHex4();
                    if (cp >= 0xD800 && cp <= 0xDBFF) {
                        // 高代理项，后随 \uXXXX 低代理项
                        if (!(consume('\\') && consume('u'))) fail("expected low surrogate");
                        uint32_t lo = parseHex4();
                        if (lo < 0xDC00 || lo > 0xDFFF) fail("invalid low surrogate");
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
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
};

void writeString(std::string& out, const std::string& s) {
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

void writeNumber(std::string& out, double d) {
    if (!std::isfinite(d)) {
        // 合法 JSON 不能表示 inf/nan；以 null 兜底（正常流程不会出现，
        // 非有限坐标会在拓扑校验阶段被标记为退化问题）。
        out += "null";
        return;
    }
    char buf[64];
    // 17 位有效数字可保证 double 的往返精度；%g 自动去掉无意义尾零。
    std::snprintf(buf, sizeof(buf), "%.17g", d);
    out += buf;
}

// 坐标数组紧凑输出（单行），其他数组/对象按缩进输出。
bool isFlatNumberArray(const Value& v) {
    if (!v.isArray() || v.arrayValue.empty()) return false;
    for (const Value& e : v.arrayValue) {
        if (!e.isNumber()) return false;
    }
    return true;
}

void writeValue(std::string& out, const Value& v, int depth, int indent) {
    const std::string pad(static_cast<size_t>(depth) * indent, ' ');
    const std::string childPad(static_cast<size_t>(depth + 1) * indent, ' ');
    switch (v.type) {
        case Value::Type::Null: out += "null"; break;
        case Value::Type::Bool: out += v.boolValue ? "true" : "false"; break;
        case Value::Type::Number: writeNumber(out, v.numberValue); break;
        case Value::Type::String: writeString(out, v.stringValue); break;
        case Value::Type::Array: {
            if (v.arrayValue.empty()) { out += "[]"; break; }
            if (isFlatNumberArray(v)) {
                out += '[';
                for (size_t i = 0; i < v.arrayValue.size(); ++i) {
                    if (i) out += ", ";
                    writeNumber(out, v.arrayValue[i].numberValue);
                }
                out += ']';
                break;
            }
            out += "[\n";
            for (size_t i = 0; i < v.arrayValue.size(); ++i) {
                out += childPad;
                writeValue(out, v.arrayValue[i], depth + 1, indent);
                if (i + 1 < v.arrayValue.size()) out += ',';
                out += '\n';
            }
            out += pad;
            out += ']';
            break;
        }
        case Value::Type::Object: {
            if (v.keys.empty()) { out += "{}"; break; }
            out += "{\n";
            for (size_t i = 0; i < v.keys.size(); ++i) {
                const std::string& k = v.keys[i];
                out += childPad;
                writeString(out, k);
                out += ": ";
                writeValue(out, v.members.at(k), depth + 1, indent);
                if (i + 1 < v.keys.size()) out += ',';
                out += '\n';
            }
            out += pad;
            out += '}';
            break;
        }
    }
}

} // namespace

Value parse(const std::string& text) {
    Parser p(text);
    return p.parseDocument();
}

std::string dump(const Value& value, int indent) {
    std::string out;
    writeValue(out, value, 0, indent);
    out.push_back('\n');
    return out;
}

} // namespace json
