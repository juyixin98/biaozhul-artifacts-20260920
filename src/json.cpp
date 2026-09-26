#include "json.hpp"

#include <cmath>
#include <cstdio>
#include <sstream>

namespace json {

// Out-of-line constructors (see rationale in json.hpp).
Value::Value() : value_(nullptr) {}
Value::Value(std::nullptr_t) : value_(nullptr) {}
Value::Value(bool b) : value_(b) {}
Value::Value(long long i) : value_(i) {}
Value::Value(int i) : value_(static_cast<long long>(i)) {}
Value::Value(long i) : value_(static_cast<long long>(i)) {}
Value::Value(double d) : value_(d) {}
Value::Value(const char* s) : value_(std::string(s)) {}
Value::Value(std::string s) : value_(std::move(s)) {}
Value::Value(ArrayT a) : value_(std::move(a)) {}
Value::Value(ObjectT o) : value_(std::move(o)) {}

namespace {

class Parser {
public:
    explicit Parser(const std::string& text) : s_(text) {}

    Value parse() {
        skipWs();
        Value v = parseValue();
        skipWs();
        if (pos_ != s_.size()) {
            throw std::runtime_error("json: trailing characters at position " +
                                     std::to_string(pos_));
        }
        return v;
    }

private:
    const std::string& s_;
    size_t pos_ = 0;

    [[noreturn]] void fail(const std::string& msg) {
        throw std::runtime_error("json: parse error at position " +
                                 std::to_string(pos_) + ": " + msg);
    }
    char peek() { return pos_ < s_.size() ? s_[pos_] : '\0'; }
    char get() { return pos_ < s_.size() ? s_[pos_++] : '\0'; }
    void expect(char c) {
        if (get() != c) fail(std::string("expected '") + c + "'");
    }
    void skipWs() {
        while (pos_ < s_.size()) {
            char c = s_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                ++pos_;
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
            case 't':
            case 'f': return parseBool();
            case 'n': return parseNull();
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
                fail("unexpected character");
        }
    }

    Value parseObject() {
        expect('{');
        Value::ObjectT out;
        skipWs();
        if (peek() == '}') { get(); return Value(std::move(out)); }
        while (true) {
            skipWs();
            if (peek() != '"') fail("expected string key");
            std::string key = parseString();
            skipWs();
            expect(':');
            Value v = parseValue();
            out.emplace(std::move(key), std::move(v));
            skipWs();
            char c = get();
            if (c == ',') continue;
            if (c == '}') break;
            fail("expected ',' or '}'");
        }
        return Value(std::move(out));
    }

    Value parseArray() {
        expect('[');
        Value::ArrayT out;
        skipWs();
        if (peek() == ']') { get(); return Value(std::move(out)); }
        while (true) {
            out.push_back(parseValue());
            skipWs();
            char c = get();
            if (c == ',') continue;
            if (c == ']') break;
            fail("expected ',' or ']'");
        }
        return Value(std::move(out));
    }

    std::string parseString() {
        expect('"');
        std::string out;
        while (true) {
            char c = get();
            if (c == '\0') fail("unterminated string");
            if (c == '"') break;
            if (c == '\\') {
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
                            if (get() != '\\' || get() != 'u')
                                fail("expected low surrogate");
                            unsigned lo = parseHex4();
                            if (lo < 0xDC00 || lo > 0xDFFF)
                                fail("invalid low surrogate");
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        }
                        appendUtf8(out, cp);
                        break;
                    }
                    default: fail("invalid escape");
                }
            } else {
                out.push_back(c);
            }
        }
        return out;
    }

    unsigned parseHex4() {
        unsigned v = 0;
        for (int i = 0; i < 4; ++i) {
            char c = get();
            v <<= 4;
            if (c >= '0' && c <= '9') v |= static_cast<unsigned>(c - '0');
            else if (c >= 'a' && c <= 'f') v |= static_cast<unsigned>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') v |= static_cast<unsigned>(c - 'A' + 10);
            else fail("invalid unicode escape");
        }
        return v;
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

    Value parseBool() {
        if (s_.compare(pos_, 4, "true") == 0) { pos_ += 4; return Value(true); }
        if (s_.compare(pos_, 5, "false") == 0) { pos_ += 5; return Value(false); }
        fail("invalid literal");
    }

    Value parseNull() {
        if (s_.compare(pos_, 4, "null") == 0) { pos_ += 4; return Value(nullptr); }
        fail("invalid literal");
    }

    Value parseNumber() {
        size_t start = pos_;
        bool isDouble = false;
        if (peek() == '-') get();
        while (true) {
            char c = peek();
            if (c >= '0' && c <= '9') { get(); }
            else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                isDouble = true;
                get();
            } else {
                break;
            }
        }
        std::string tok = s_.substr(start, pos_ - start);
        if (isDouble) return Value(std::stod(tok));
        try {
            return Value(static_cast<long long>(std::stoll(tok)));
        } catch (...) {
            return Value(std::stod(tok));
        }
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
                } else {
                    out.push_back(static_cast<char>(c));
                }
        }
    }
    out.push_back('"');
}

void dumpDouble(double d, std::string& out) {
    if (std::isfinite(d)) {
        char buf[40];
        std::snprintf(buf, sizeof(buf), "%.17g", d);
        out += buf;
    } else {
        out += "null";  // JSON has no Infinity/NaN
    }
}

void dumpTo(const Value& v, std::string& out, int indent, int depth) {
    auto nl = [&](int d) {
        if (indent > 0) {
            out.push_back('\n');
            out.append(static_cast<size_t>(indent) * d, ' ');
        }
    };
    switch (v.type()) {
        case Value::Null: out += "null"; break;
        case Value::Bool: out += v.asBool() ? "true" : "false"; break;
        case Value::Int: out += std::to_string(v.asInt()); break;
        case Value::Double: dumpDouble(v.asDouble(), out); break;
        case Value::String: dumpString(v.asString(), out); break;
        case Value::Array: {
            const auto& a = v.asArray();
            if (a.empty()) { out += "[]"; break; }
            out.push_back('[');
            for (size_t i = 0; i < a.size(); ++i) {
                if (i) out.push_back(',');
                nl(depth + 1);
                dumpTo(a[i], out, indent, depth + 1);
            }
            nl(depth);
            out.push_back(']');
            break;
        }
        case Value::Object: {
            const auto& o = v.asObject();
            if (o.empty()) { out += "{}"; break; }
            out.push_back('{');
            size_t i = 0;
            for (const auto& [k, val] : o) {
                if (i++) out.push_back(',');
                nl(depth + 1);
                dumpString(k, out);
                out += indent > 0 ? ": " : ":";
                dumpTo(val, out, indent, depth + 1);
            }
            nl(depth);
            out.push_back('}');
            break;
        }
    }
}

}  // namespace

Value parse(const std::string& text) { return Parser(text).parse(); }

std::string dump(const Value& v, int indent) {
    std::string out;
    dumpTo(v, out, indent, 0);
    out.push_back('\n');
    return out;
}

}  // namespace json
