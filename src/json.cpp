#include "topp/json.hpp"
#include <charconv>
#include <cmath>
#include <sstream>

namespace topp {

namespace {

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

// Canonical number spelling: 17 significant digits ('%.17g'), which round
// trips every IEEE-754 double and matches Python's "repr-normalized" form
// used by the checksum verifier. Deterministic across platforms.
void dumpNumber(double d, std::string& out) {
    if (std::isnan(d) || std::isinf(d))
        throw std::runtime_error("cannot serialize non-finite number");
    if (d == 0.0) d = std::abs(d);  // normalize -0.0 -> +0.0 canonically
    char buf[40];
    int n = std::snprintf(buf, sizeof(buf), "%.17g", d);
    if (n > 0) {
        out.append(buf, static_cast<size_t>(n));
    } else {
        std::ostringstream oss;
        oss.precision(17);
        oss << d;
        out += oss.str();
    }
}

void dumpInto(const JsonValue& v, std::string& out) {
    switch (v.type()) {
        case JsonValue::Type::Null: out += "null"; break;
        case JsonValue::Type::Bool: out += v.asBool() ? "true" : "false"; break;
        case JsonValue::Type::Number: dumpNumber(v.asNumber(), out); break;
        case JsonValue::Type::String: dumpString(v.asString(), out); break;
        case JsonValue::Type::Array: {
            out.push_back('[');
            bool first = true;
            for (const auto& item : v.asArray()) {
                if (!first) out.push_back(',');
                first = false;
                dumpInto(item, out);
            }
            out.push_back(']');
            break;
        }
        case JsonValue::Type::Object: {
            out.push_back('{');
            bool first = true;
            for (const auto& [k, val] : v.asObject()) {
                if (!first) out.push_back(',');
                first = false;
                dumpString(k, out);
                out.push_back(':');
                dumpInto(val, out);
            }
            out.push_back('}');
            break;
        }
    }
}

class Parser {
public:
    explicit Parser(const std::string& text) : t_(text), pos_(0) {}

    JsonValue parse() {
        skipWs();
        JsonValue v = parseValue();
        skipWs();
        if (pos_ != t_.size())
            fail("trailing characters after JSON value");
        return v;
    }

private:
    const std::string& t_;
    size_t pos_;

    [[noreturn]] void fail(const std::string& msg) {
        throw std::runtime_error("JSON parse error at offset " +
                                 std::to_string(pos_) + ": " + msg);
    }

    char peek() {
        if (pos_ >= t_.size()) fail("unexpected end of input");
        return t_[pos_];
    }
    char get() {
        if (pos_ >= t_.size()) fail("unexpected end of input");
        return t_[pos_++];
    }
    void expect(char c) {
        if (get() != c) fail(std::string("expected '") + c + "'");
    }
    void skipWs() {
        while (pos_ < t_.size()) {
            char c = t_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos_;
            else break;
        }
    }

    JsonValue parseValue() {
        skipWs();
        char c = peek();
        switch (c) {
            case '{': return parseObject();
            case '[': return parseArray();
            case '"': return JsonValue(parseString());
            case 't': case 'f': return parseBool();
            case 'n': return parseNull();
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
                fail("unexpected character");
        }
    }

    JsonValue parseObject() {
        auto v = JsonValue::object();
        expect('{');
        skipWs();
        if (peek() == '}') { get(); return v; }
        while (true) {
            skipWs();
            std::string key = parseString();
            skipWs();
            expect(':');
            JsonValue val = parseValue();
            v.asObject()[key] = std::move(val);
            skipWs();
            char c = get();
            if (c == '}') break;
            if (c != ',') fail("expected ',' or '}'");
        }
        return v;
    }

    JsonValue parseArray() {
        auto v = JsonValue::array();
        expect('[');
        skipWs();
        if (peek() == ']') { get(); return v; }
        while (true) {
            v.asArray().push_back(parseValue());
            skipWs();
            char c = get();
            if (c == ']') break;
            if (c != ',') fail("expected ',' or ']'");
        }
        return v;
    }

    std::string parseString() {
        expect('"');
        std::string s;
        while (true) {
            char c = get();
            if (c == '"') break;
            if (c == '\\') {
                char e = get();
                switch (e) {
                    case '"': s.push_back('"'); break;
                    case '\\': s.push_back('\\'); break;
                    case '/': s.push_back('/'); break;
                    case 'b': s.push_back('\b'); break;
                    case 'f': s.push_back('\f'); break;
                    case 'n': s.push_back('\n'); break;
                    case 'r': s.push_back('\r'); break;
                    case 't': s.push_back('\t'); break;
                    case 'u': {
                        unsigned code = 0;
                        for (int i = 0; i < 4; ++i) {
                            char h = get();
                            code <<= 4;
                            if (h >= '0' && h <= '9') code |= h - '0';
                            else if (h >= 'a' && h <= 'f') code |= h - 'a' + 10;
                            else if (h >= 'A' && h <= 'F') code |= h - 'A' + 10;
                            else fail("bad unicode escape");
                        }
                        // UTF-8 encode (surrogates paired when both halves present)
                        if (code >= 0xD800 && code <= 0xDBFF) {
                            if (get() != '\\' || get() != 'u') fail("expected low surrogate");
                            unsigned low = 0;
                            for (int i = 0; i < 4; ++i) {
                                char h = get();
                                low <<= 4;
                                if (h >= '0' && h <= '9') low |= h - '0';
                                else if (h >= 'a' && h <= 'f') low |= h - 'a' + 10;
                                else if (h >= 'A' && h <= 'F') low |= h - 'A' + 10;
                                else fail("bad unicode escape");
                            }
                            code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
                        }
                        if (code < 0x80) {
                            s.push_back(static_cast<char>(code));
                        } else if (code < 0x800) {
                            s.push_back(static_cast<char>(0xC0 | (code >> 6)));
                            s.push_back(static_cast<char>(0x80 | (code & 0x3F)));
                        } else if (code < 0x10000) {
                            s.push_back(static_cast<char>(0xE0 | (code >> 12)));
                            s.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
                            s.push_back(static_cast<char>(0x80 | (code & 0x3F)));
                        } else {
                            s.push_back(static_cast<char>(0xF0 | (code >> 18)));
                            s.push_back(static_cast<char>(0x80 | ((code >> 12) & 0x3F)));
                            s.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
                            s.push_back(static_cast<char>(0x80 | (code & 0x3F)));
                        }
                        break;
                    }
                    default: fail("bad escape");
                }
            } else if (static_cast<unsigned char>(c) < 0x20) {
                fail("unescaped control character");
            } else {
                s.push_back(c);
            }
        }
        return s;
    }

    JsonValue parseBool() {
        if (t_.compare(pos_, 4, "true") == 0) { pos_ += 4; return JsonValue(true); }
        if (t_.compare(pos_, 5, "false") == 0) { pos_ += 5; return JsonValue(false); }
        fail("invalid literal");
    }

    JsonValue parseNull() {
        if (t_.compare(pos_, 4, "null") == 0) { pos_ += 4; return JsonValue(); }
        fail("invalid literal");
    }

    JsonValue parseNumber() {
        size_t start = pos_;
        if (peek() == '-') get();
        while (pos_ < t_.size()) {
            char c = t_[pos_];
            if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' ||
                c == '+' || c == '-')
                ++pos_;
            else break;
        }
        try {
            return JsonValue(std::stod(t_.substr(start, pos_ - start)));
        } catch (...) {
            fail("invalid number");
        }
    }
};

} // namespace

std::string JsonValue::dump() const {
    std::string out;
    dumpInto(*this, out);
    return out;
}

JsonValue JsonValue::parse(const std::string& text) {
    return Parser(text).parse();
}

} // namespace topp
