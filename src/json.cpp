#include "json.hpp"

#include <cmath>
#include <cstdio>
#include <sstream>

namespace json {

namespace {

class Parser {
public:
    explicit Parser(const std::string& text) : s_(text), i_(0) {}

    Value parse() {
        skipWs();
        Value v = parseValue();
        skipWs();
        if (i_ != s_.size()) {
            throw ParseError("unexpected trailing characters", i_);
        }
        return v;
    }

private:
    const std::string& s_;
    size_t i_;

    [[noreturn]] void fail(const std::string& msg) { throw ParseError(msg, i_); }

    void skipWs() {
        while (i_ < s_.size()) {
            char c = s_[i_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                ++i_;
            } else {
                break;
            }
        }
    }

    char peek() {
        if (i_ >= s_.size()) fail("unexpected end of input");
        return s_[i_];
    }

    Value parseValue() {
        skipWs();
        if (i_ >= s_.size()) fail("unexpected end of input");
        char c = s_[i_];
        switch (c) {
            case '{': return parseObject();
            case '[': return parseArray();
            case '"': return Value(parseString());
            case 't': case 'f': return parseBool();
            case 'n': return parseNull();
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
                fail("unexpected character");
        }
    }

    Value parseObject() {
        Value v = Value::object();
        ++i_;  // '{'
        skipWs();
        if (peek() == '}') { ++i_; return v; }
        while (true) {
            skipWs();
            if (peek() != '"') fail("expected string key in object");
            std::string key = parseString();
            skipWs();
            if (peek() != ':') fail("expected ':' in object");
            ++i_;
            Value val = parseValue();
            v.objectMembers().emplace_back(key, val);
            skipWs();
            char c = peek();
            if (c == ',') { ++i_; continue; }
            if (c == '}') { ++i_; break; }
            fail("expected ',' or '}' in object");
        }
        return v;
    }

    Value parseArray() {
        Value v = Value::array();
        ++i_;  // '['
        skipWs();
        if (peek() == ']') { ++i_; return v; }
        while (true) {
            Value item = parseValue();
            v.push_back(item);
            skipWs();
            char c = peek();
            if (c == ',') { ++i_; skipWs(); continue; }
            if (c == ']') { ++i_; break; }
            fail("expected ',' or ']' in array");
        }
        return v;
    }

    std::string parseString() {
        ++i_;  // opening quote
        std::string out;
        while (true) {
            if (i_ >= s_.size()) fail("unterminated string");
            char c = s_[i_++];
            if (c == '"') break;
            if (c == '\\') {
                if (i_ >= s_.size()) fail("unterminated escape");
                char e = s_[i_++];
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
                        if (i_ + 4 > s_.size()) fail("bad \\u escape");
                        unsigned code = 0;
                        for (int k = 0; k < 4; ++k) {
                            char h = s_[i_++];
                            code <<= 4;
                            if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
                            else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
                            else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
                            else fail("bad hex digit in \\u escape");
                        }
                        appendUtf8(out, code);
                        break;
                    }
                    default: fail("bad escape character");
                }
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

    Value parseBool() {
        if (s_.compare(i_, 4, "true") == 0) { i_ += 4; return Value(true); }
        if (s_.compare(i_, 5, "false") == 0) { i_ += 5; return Value(false); }
        fail("invalid literal");
    }

    Value parseNull() {
        if (s_.compare(i_, 4, "null") == 0) { i_ += 4; return Value(); }
        fail("invalid literal");
    }

    Value parseNumber() {
        size_t start = i_;
        if (peek() == '-') ++i_;
        while (i_ < s_.size()) {
            char c = s_[i_];
            if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                ++i_;
            } else {
                break;
            }
        }
        try {
            return Value(std::stod(s_.substr(start, i_ - start)));
        } catch (...) {
            fail("invalid number");
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

void dumpNumber(double d, std::string& out) {
    // Integers are emitted without a decimal point; this is all the API uses.
    if (std::isfinite(d) && d == std::floor(d) &&
        std::fabs(d) < 9.007199254740992e15) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "%lld", static_cast<long long>(d));
        out += buf;
        return;
    }
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%.17g", d);
    out += buf;
}

void dumpInto(const Value& v, std::string& out, bool indent, int depth) {
    std::string pad(indent ? static_cast<size_t>(depth + 1) * 2 : 0, ' ');
    std::string closePad(indent ? static_cast<size_t>(depth) * 2 : 0, ' ');
    switch (v.type()) {
        case Value::Type::Null: out += "null"; break;
        case Value::Type::Bool: out += v.asBool() ? "true" : "false"; break;
        case Value::Type::Number: dumpNumber(v.asNumber(), out); break;
        case Value::Type::String: dumpString(v.asString(), out); break;
        case Value::Type::Array: {
            const auto& items = v.asArray();
            if (items.empty()) { out += "[]"; break; }
            out.push_back('[');
            for (size_t k = 0; k < items.size(); ++k) {
                if (indent) { out.push_back('\n'); out += pad; }
                dumpInto(items[k], out, indent, depth + 1);
                if (k + 1 < items.size()) out.push_back(',');
            }
            if (indent) { out.push_back('\n'); out += closePad; }
            out.push_back(']');
            break;
        }
        case Value::Type::Object: {
            const auto& members = v.asObject();
            if (members.empty()) { out += "{}"; break; }
            out.push_back('{');
            for (size_t k = 0; k < members.size(); ++k) {
                if (indent) { out.push_back('\n'); out += pad; }
                dumpString(members[k].first, out);
                out.push_back(':');
                if (indent) out.push_back(' ');
                dumpInto(members[k].second, out, indent, depth + 1);
                if (k + 1 < members.size()) out.push_back(',');
            }
            if (indent) { out.push_back('\n'); out += closePad; }
            out.push_back('}');
            break;
        }
    }
}

}  // namespace

Value parse(const std::string& text) {
    return Parser(text).parse();
}

std::string dump(const Value& v, bool indent) {
    std::string out;
    dumpInto(v, out, indent, 0);
    return out;
}

}  // namespace json
