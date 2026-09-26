#include "json.hpp"

#include <cmath>
#include <cstdio>
#include <limits>
#include <sstream>

namespace json {

namespace {

class Parser {
public:
    explicit Parser(const std::string& text) : s_(text), pos_(0) {}

    Value parse() {
        skipWs();
        Value v = parseValue();
        skipWs();
        if (pos_ != s_.size()) fail("trailing characters after JSON value");
        return v;
    }

private:
    const std::string& s_;
    std::size_t pos_;

    [[noreturn]] void fail(const std::string& msg) {
        throw Error("JSON parse error at offset " + std::to_string(pos_) + ": " + msg);
    }

    char peek() {
        if (pos_ >= s_.size()) fail("unexpected end of input");
        return s_[pos_];
    }

    void skipWs() {
        while (pos_ < s_.size()) {
            char c = s_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos_;
            else break;
        }
    }

    bool consumeLiteral(const char* lit) {
        std::size_t n = 0;
        while (lit[n]) ++n;
        if (s_.compare(pos_, n, lit) == 0) { pos_ += n; return true; }
        return false;
    }

    Value parseValue() {
        skipWs();
        if (pos_ >= s_.size()) fail("expected value");
        char c = s_[pos_];
        if (c == '{') return parseObject();
        if (c == '[') return parseArray();
        if (c == '"') { Value v; v.type = Value::String; v.str = parseString(); return v; }
        if (c == 't' || c == 'f') {
            bool b;
            if (consumeLiteral("true")) b = true;
            else if (consumeLiteral("false")) b = false;
            else fail("invalid literal");
            Value v; v.type = Value::Bool; v.boolean = b; return v;
        }
        if (c == 'n') {
            if (!consumeLiteral("null")) fail("invalid literal");
            return Value();
        }
        return parseNumber();
    }

    Value parseObject() {
        Value v; v.type = Value::Object;
        ++pos_; // '{'
        skipWs();
        if (peek() == '}') { ++pos_; return v; }
        for (;;) {
            skipWs();
            if (peek() != '"') fail("expected string key in object");
            std::string key = parseString();
            skipWs();
            if (peek() != ':') fail("expected ':' after object key");
            ++pos_;
            Value val = parseValue();
            v.obj.emplace_back(std::move(key), std::move(val));
            skipWs();
            char c = peek();
            if (c == ',') { ++pos_; continue; }
            if (c == '}') { ++pos_; break; }
            fail("expected ',' or '}' in object");
        }
        return v;
    }

    Value parseArray() {
        Value v; v.type = Value::Array;
        ++pos_; // '['
        skipWs();
        if (peek() == ']') { ++pos_; return v; }
        for (;;) {
            v.arr.push_back(parseValue());
            skipWs();
            char c = peek();
            if (c == ',') { ++pos_; continue; }
            if (c == ']') { ++pos_; break; }
            fail("expected ',' or ']' in array");
        }
        return v;
    }

    std::string parseString() {
        ++pos_; // opening quote
        std::string out;
        for (;;) {
            if (pos_ >= s_.size()) fail("unterminated string");
            char c = s_[pos_++];
            if (c == '"') break;
            if (c == '\\') {
                if (pos_ >= s_.size()) fail("unterminated escape");
                char e = s_[pos_++];
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
                        if (pos_ + 4 > s_.size()) fail("bad \\u escape");
                        unsigned code = 0;
                        for (int i = 0; i < 4; ++i) {
                            char h = s_[pos_++];
                            code <<= 4;
                            if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
                            else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
                            else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
                            else fail("bad hex digit in \\u escape");
                        }
                        // Encode code point (surrogates must come in pairs) as UTF-8.
                        if (code >= 0xD800 && code <= 0xDBFF) {
                            if (pos_ + 6 <= s_.size() && s_[pos_] == '\\' && s_[pos_ + 1] == 'u') {
                                pos_ += 2;
                                unsigned lo = 0;
                                for (int i = 0; i < 4; ++i) {
                                    char h = s_[pos_++];
                                    lo <<= 4;
                                    if (h >= '0' && h <= '9') lo |= static_cast<unsigned>(h - '0');
                                    else if (h >= 'a' && h <= 'f') lo |= static_cast<unsigned>(h - 'a' + 10);
                                    else if (h >= 'A' && h <= 'F') lo |= static_cast<unsigned>(h - 'A' + 10);
                                    else fail("bad hex digit in surrogate");
                                }
                                if (lo >= 0xDC00 && lo <= 0xDFFF) {
                                    code = 0x10000 + ((code - 0xD800) << 10) + (lo - 0xDC00);
                                } else {
                                    fail("unpaired surrogate");
                                }
                            } else {
                                fail("unpaired high surrogate");
                            }
                        } else if (code >= 0xDC00 && code <= 0xDFFF) {
                            fail("unexpected low surrogate");
                        }
                        appendUtf8(out, code);
                        break;
                    }
                    default: fail("invalid escape character");
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
        } else if (code < 0x10000) {
            out.push_back(static_cast<char>(0xE0 | (code >> 12)));
            out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
        } else {
            out.push_back(static_cast<char>(0xF0 | (code >> 18)));
            out.push_back(static_cast<char>(0x80 | ((code >> 12) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
        }
    }

    Value parseNumber() {
        std::size_t start = pos_;
        if (peek() == '-') ++pos_;
        auto digits = [&] {
            std::size_t begin = pos_;
            while (pos_ < s_.size() && s_[pos_] >= '0' && s_[pos_] <= '9') ++pos_;
            return pos_ > begin;
        };
        if (!digits()) fail("invalid number");
        bool isReal = false;
        if (pos_ < s_.size() && s_[pos_] == '.') {
            isReal = true;
            ++pos_;
            if (!digits()) fail("invalid fraction");
        }
        if (pos_ < s_.size() && (s_[pos_] == 'e' || s_[pos_] == 'E')) {
            isReal = true;
            ++pos_;
            if (pos_ < s_.size() && (s_[pos_] == '+' || s_[pos_] == '-')) ++pos_;
            if (!digits()) fail("invalid exponent");
        }
        std::string token = s_.substr(start, pos_ - start);
        Value v;
        v.type = Value::Number;
        try {
            v.number = std::stod(token);
        } catch (...) {
            fail("number out of range");
        }
        (void)isReal;
        return v;
    }
};

void appendEscaped(std::string& out, const std::string& str) {
    out.push_back('"');
    for (unsigned char c : str) {
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

} // namespace

Value Value::parse(const std::string& text) {
    Parser parser(text);
    return parser.parse();
}

bool Value::has(const std::string& key) const {
    if (type != Object) return false;
    for (const auto& kv : obj)
        if (kv.first == key) return true;
    return false;
}

const Value& Value::at(const std::string& key) const {
    if (type != Object) throw Error("expected object when looking up key '" + key + "'");
    for (const auto& kv : obj)
        if (kv.first == key) return kv.second;
    throw Error("missing key: " + key);
}

std::int64_t Value::asInt() const {
    if (type != Number) throw Error("expected integer, got non-number");
    if (std::isnan(number) || std::isinf(number)) throw Error("expected integer, got NaN/Inf");
    double r = std::floor(number);
    if (r != number) throw Error("expected integer, got fractional value");
    if (r < static_cast<double>(std::numeric_limits<std::int64_t>::min()) ||
        r > static_cast<double>(std::numeric_limits<std::int64_t>::max())) {
        throw Error("integer out of int64 range");
    }
    return static_cast<std::int64_t>(r);
}

void Value::set(const std::string& key, Value value) {
    for (auto& kv : obj) {
        if (kv.first == key) { kv.second = std::move(value); return; }
    }
    obj.emplace_back(key, std::move(value));
}

void Value::push(Value value) {
    arr.push_back(std::move(value));
}

std::string Value::dump() const {
    std::string out;
    dumpTo(out, 0, 0);
    return out;
}

std::string Value::dumpPretty(int indent) const {
    std::string out;
    dumpTo(out, 0, indent);
    return out;
}

void Value::dumpTo(std::string& out, int depth, int indent) const {
    auto pad = [&](int d) {
        if (indent > 0) out.append(static_cast<std::size_t>(d * indent), ' ');
    };
    switch (type) {
        case Null: out += "null"; break;
        case Bool: out += boolean ? "true" : "false"; break;
        case Number: {
            if (std::isnan(number) || std::isinf(number)) {
                // JSON cannot represent NaN/Inf; emit null rather than corrupt output.
                out += "null";
                break;
            }
            double r = std::floor(number);
            if (r == number && std::fabs(number) < 9.007199254740992e15) {
                std::int64_t iv = static_cast<std::int64_t>(number);
                out += std::to_string(iv);
            } else {
                std::ostringstream ss;
                ss << number;
                out += ss.str();
            }
            break;
        }
        case String: appendEscaped(out, str); break;
        case Array:
            if (arr.empty()) { out += "[]"; break; }
            out.push_back('[');
            if (indent > 0) out.push_back('\n');
            for (std::size_t i = 0; i < arr.size(); ++i) {
                if (indent > 0) pad(depth + 1);
                arr[i].dumpTo(out, depth + 1, indent);
                if (i + 1 < arr.size()) out.push_back(',');
                if (indent > 0) out.push_back('\n');
            }
            pad(depth);
            out.push_back(']');
            break;
        case Object:
            if (obj.empty()) { out += "{}"; break; }
            out.push_back('{');
            if (indent > 0) out.push_back('\n');
            for (std::size_t i = 0; i < obj.size(); ++i) {
                if (indent > 0) pad(depth + 1);
                appendEscaped(out, obj[i].first);
                out.push_back(':');
                if (indent > 0) out.push_back(' ');
                obj[i].second.dumpTo(out, depth + 1, indent);
                if (i + 1 < obj.size()) out.push_back(',');
                if (indent > 0) out.push_back('\n');
            }
            pad(depth);
            out.push_back('}');
            break;
    }
}

} // namespace json
