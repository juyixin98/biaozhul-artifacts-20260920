// json.cpp - 递归下降 JSON 解析器与序列化
#include "json.hpp"

#include <cmath>
#include <cstdio>
#include <sstream>

namespace json {

namespace {

struct Parser {
    const std::string& s;
    size_t pos = 0;

    explicit Parser(const std::string& text) : s(text) {}

    [[noreturn]] void fail(const std::string& msg) {
        size_t line = 1, col = 1;
        for (size_t i = 0; i < pos && i < s.size(); ++i) {
            if (s[i] == '\n') {
                ++line;
                col = 1;
            } else {
                ++col;
            }
        }
        std::ostringstream os;
        os << msg << " at line " << line << ", col " << col;
        throw std::runtime_error(os.str());
    }

    void skipWs() {
        while (pos < s.size()) {
            char c = s[pos];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r')
                ++pos;
            else
                break;
        }
    }

    char peek() {
        if (pos >= s.size()) fail("unexpected end of input");
        return s[pos];
    }

    Value parseValue() {
        skipWs();
        char c = peek();
        switch (c) {
            case '{':
                return parseObject();
            case '[':
                return parseArray();
            case '"':
                return Value::makeStr(parseString());
            case 't':
            case 'f':
                return parseBool();
            case 'n':
                return parseNull();
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
                fail(std::string("unexpected character '") + c + "'");
        }
    }

    Value parseObject() {
        Value v = Value::makeObj();
        ++pos;  // {
        skipWs();
        if (peek() == '}') {
            ++pos;
            return v;
        }
        while (true) {
            skipWs();
            if (peek() != '"') fail("expected string key in object");
            std::string key = parseString();
            skipWs();
            if (peek() != ':') fail("expected ':' after object key");
            ++pos;
            v.obj[key] = parseValue();
            skipWs();
            char c = peek();
            if (c == ',') {
                ++pos;
                continue;
            }
            if (c == '}') {
                ++pos;
                break;
            }
            fail("expected ',' or '}' in object");
        }
        return v;
    }

    Value parseArray() {
        Value v = Value::makeArr();
        ++pos;  // [
        skipWs();
        if (peek() == ']') {
            ++pos;
            return v;
        }
        while (true) {
            v.arr.push_back(parseValue());
            skipWs();
            char c = peek();
            if (c == ',') {
                ++pos;
                continue;
            }
            if (c == ']') {
                ++pos;
                break;
            }
            fail("expected ',' or ']' in array");
        }
        return v;
    }

    std::string parseString() {
        ++pos;  // opening quote
        std::string out;
        while (pos < s.size()) {
            char c = s[pos++];
            if (c == '"') return out;
            if (c == '\\') {
                if (pos >= s.size()) fail("bad escape at end of input");
                char e = s[pos++];
                switch (e) {
                    case '"':
                        out.push_back('"');
                        break;
                    case '\\':
                        out.push_back('\\');
                        break;
                    case '/':
                        out.push_back('/');
                        break;
                    case 'b':
                        out.push_back('\b');
                        break;
                    case 'f':
                        out.push_back('\f');
                        break;
                    case 'n':
                        out.push_back('\n');
                        break;
                    case 'r':
                        out.push_back('\r');
                        break;
                    case 't':
                        out.push_back('\t');
                        break;
                    case 'u': {
                        if (pos + 4 > s.size()) fail("bad \\u escape");
                        unsigned code = 0;
                        for (int i = 0; i < 4; ++i) {
                            char h = s[pos++];
                            code <<= 4;
                            if (h >= '0' && h <= '9')
                                code |= h - '0';
                            else if (h >= 'a' && h <= 'f')
                                code |= h - 'a' + 10;
                            else if (h >= 'A' && h <= 'F')
                                code |= h - 'A' + 10;
                            else
                                fail("bad hex digit in \\u escape");
                        }
                        appendUtf8(out, code);
                        break;
                    }
                    default:
                        fail("invalid escape character");
                }
            } else if (static_cast<unsigned char>(c) < 0x20) {
                fail("unescaped control character in string");
            } else {
                out.push_back(c);
            }
        }
        fail("unterminated string");
    }

    static void appendUtf8(std::string& out, unsigned cp) {
        if (cp < 0x80) {
            out.push_back(static_cast<char>(cp));
        } else if (cp < 0x800) {
            out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        } else {
            out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        }
    }

    Value parseBool() {
        if (s.compare(pos, 4, "true") == 0) {
            pos += 4;
            return Value::makeBool(true);
        }
        if (s.compare(pos, 5, "false") == 0) {
            pos += 5;
            return Value::makeBool(false);
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
        size_t start = pos;
        if (peek() == '-') ++pos;
        auto digits = [&] {
            size_t at = pos;
            while (pos < s.size() && s[pos] >= '0' && s[pos] <= '9') ++pos;
            return pos - at;
        };
        if (peek() == '0') {
            ++pos;  // 不允许多位前导零
        } else {
            if (digits() == 0) fail("invalid number");
        }
        if (pos < s.size() && s[pos] == '.') {
            ++pos;
            if (digits() == 0) fail("fraction part requires digits");
        }
        if (pos < s.size() && (s[pos] == 'e' || s[pos] == 'E')) {
            ++pos;
            if (pos < s.size() && (s[pos] == '+' || s[pos] == '-')) ++pos;
            if (digits() == 0) fail("exponent requires digits");
        }
        std::string tok = s.substr(start, pos - start);
        try {
            size_t used = 0;
            double d = std::stod(tok, &used);
            if (used != tok.size()) fail("invalid number");
            if (!std::isfinite(d)) fail("number out of finite range");
            return Value::makeNum(d);
        } catch (const std::exception&) {
            fail("invalid number");
        }
    }
};

void dumpString(std::string& out, const std::string& str) {
    out.push_back('"');
    for (unsigned char c : str) {
        switch (c) {
            case '"':
                out += "\\\"";
                break;
            case '\\':
                out += "\\\\";
                break;
            case '\b':
                out += "\\b";
                break;
            case '\f':
                out += "\\f";
                break;
            case '\n':
                out += "\\n";
                break;
            case '\r':
                out += "\\r";
                break;
            case '\t':
                out += "\\t";
                break;
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

void dumpValue(std::string& out, const Value& v, int depth, int indent) {
    auto pad = [&](int d) {
        if (indent > 0)
            out.append(static_cast<size_t>(d * indent), ' ');
    };
    switch (v.type) {
        case Type::Null:
            out += "null";
            break;
        case Type::Bool:
            out += v.b ? "true" : "false";
            break;
        case Type::Number: {
            double d = v.num;
            if (!std::isfinite(d)) {
                out += "null";  // 防御: JSON 不能表示 NaN/Infinity
            } else if (d == 0.0) {
                out += std::signbit(d) ? "-0" : "0";
            } else {
                // 优先紧凑表示: 若 %.15g 能往返该 double 则使用之,
                // 否则退回 %.17g 保证精确往返。
                char compact[64], exact[64];
                std::snprintf(compact, sizeof(compact), "%.15g", d);
                std::snprintf(exact, sizeof(exact), "%.17g", d);
                if (std::stod(compact) == d)
                    out += compact;
                else
                    out += exact;
            }
            break;
        }
        case Type::String:
            dumpString(out, v.str);
            break;
        case Type::Array:
            if (v.arr.empty()) {
                out += "[]";
                break;
            }
            out += indent > 0 ? "[\n" : "[";
            for (size_t i = 0; i < v.arr.size(); ++i) {
                pad(depth + 1);
                dumpValue(out, v.arr[i], depth + 1, indent);
                if (i + 1 < v.arr.size()) out += ",";
                if (indent > 0) out.push_back('\n');
            }
            pad(depth);
            out.push_back(']');
            break;
        case Type::Object:
            if (v.obj.empty()) {
                out += "{}";
                break;
            }
            out += indent > 0 ? "{\n" : "{";
            size_t i = 0;
            for (const auto& [k, val] : v.obj) {
                pad(depth + 1);
                dumpString(out, k);
                out += indent > 0 ? ": " : ":";
                dumpValue(out, val, depth + 1, indent);
                if (++i < v.obj.size()) out += ",";
                if (indent > 0) out.push_back('\n');
            }
            pad(depth);
            out.push_back('}');
            break;
    }
}

}  // namespace

ParseResult parse(const std::string& text) {
    ParseResult r;
    try {
        Parser p(text);
        r.value = p.parseValue();
        p.skipWs();
        if (p.pos != text.size()) p.fail("trailing characters after JSON value");
        r.ok = true;
    } catch (const std::exception& e) {
        r.ok = false;
        r.error = e.what();
    }
    return r;
}

std::string dump(const Value& v, int indent) {
    std::string out;
    dumpValue(out, v, 0, indent);
    out.push_back('\n');
    return out;
}

}  // namespace json
