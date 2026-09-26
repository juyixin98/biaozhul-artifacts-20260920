// SPDX-License-Identifier: MIT
#include "json.h"

#include <cctype>
#include <cmath>
#include <sstream>

namespace dagpaths {

namespace {

class Parser {
public:
    explicit Parser(const std::string& text) : text_(text) {}

    JsonValue parse() {
        size_t pos = 0;
        JsonValue value = parseOne(pos);
        pos_ = pos;
        skipWs();
        if (pos_ != text_.size())
            fail("trailing characters after JSON value");
        return value;
    }

    // Parses one value beginning at pos, advancing pos past it.
    JsonValue parseOne(size_t& pos) {
        pos_ = pos;
        skipWs();
        JsonValue value = parseValue();
        pos = pos_;
        return value;
    }

private:
    const std::string& text_;
    size_t pos_ = 0;

    [[noreturn]] void fail(const std::string& msg) const {
        throw std::invalid_argument(
            "JSON parse error at offset " + std::to_string(pos_) + ": " + msg);
    }

    char peek() const {
        if (pos_ >= text_.size())
            throw std::invalid_argument("JSON parse error: unexpected end of input");
        return text_[pos_];
    }

    void skipWs() {
        while (pos_ < text_.size()) {
            const char c = text_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos_;
            else break;
        }
    }

    JsonValue parseValue() {
        skipWs();
        if (pos_ >= text_.size()) fail("expected value");
        switch (text_[pos_]) {
            case '{': return parseObject();
            case '[': return parseArray();
            case '"': return JsonValue::makeString(parseString());
            case 't': case 'f': return parseBool();
            case 'n': return parseNull();
            default:
                if (text_[pos_] == '-' || std::isdigit(static_cast<unsigned char>(text_[pos_])))
                    return parseNumber();
                fail("unexpected character");
        }
    }

    JsonValue parseObject() {
        JsonValue v;
        v.type = JsonType::Object;
        ++pos_; // '{'
        skipWs();
        if (peek() == '}') { ++pos_; return v; }
        while (true) {
            skipWs();
            if (peek() != '"') fail("expected string key in object");
            std::string key = parseString();
            skipWs();
            if (peek() != ':') fail("expected ':' after object key");
            ++pos_;
            JsonValue value = parseValue();
            if (v.obj.count(key)) fail("duplicate object key: " + key);
            v.obj.emplace(std::move(key), std::move(value));
            skipWs();
            const char c = peek();
            if (c == ',') { ++pos_; continue; }
            if (c == '}') { ++pos_; break; }
            fail("expected ',' or '}' in object");
        }
        return v;
    }

    JsonValue parseArray() {
        JsonValue v;
        v.type = JsonType::Array;
        ++pos_; // '['
        skipWs();
        if (peek() == ']') { ++pos_; return v; }
        while (true) {
            v.arr.push_back(parseValue());
            skipWs();
            const char c = peek();
            if (c == ',') { ++pos_; continue; }
            if (c == ']') { ++pos_; break; }
            fail("expected ',' or ']' in array");
        }
        return v;
    }

    std::string parseString() {
        ++pos_; // opening quote
        std::string out;
        while (true) {
            if (pos_ >= text_.size())
                throw std::invalid_argument("JSON parse error: unterminated string");
            const char c = text_[pos_++];
            if (c == '"') break;
            if (c == '\\') {
                if (pos_ >= text_.size())
                    throw std::invalid_argument("JSON parse error: unterminated escape");
                const char e = text_[pos_++];
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
                        if (pos_ + 4 > text_.size())
                            throw std::invalid_argument("JSON parse error: bad \\u escape");
                        unsigned code = 0;
                        for (int i = 0; i < 4; ++i) {
                            const char h = text_[pos_++];
                            code <<= 4;
                            if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
                            else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
                            else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
                            else throw std::invalid_argument("JSON parse error: bad hex digit in \\u escape");
                        }
                        // Surrogate pair handling.
                        if (code >= 0xD800 && code <= 0xDBFF) {
                            if (pos_ + 6 <= text_.size() && text_[pos_] == '\\' && text_[pos_ + 1] == 'u') {
                                unsigned lo = 0;
                                size_t save = pos_;
                                pos_ += 2;
                                bool valid = true;
                                for (int i = 0; i < 4; ++i) {
                                    const char h = text_[pos_++];
                                    lo <<= 4;
                                    if (h >= '0' && h <= '9') lo |= static_cast<unsigned>(h - '0');
                                    else if (h >= 'a' && h <= 'f') lo |= static_cast<unsigned>(h - 'a' + 10);
                                    else if (h >= 'A' && h <= 'F') lo |= static_cast<unsigned>(h - 'A' + 10);
                                    else valid = false;
                                }
                                if (valid && lo >= 0xDC00 && lo <= 0xDFFF) {
                                    code = 0x10000 + ((code - 0xD800) << 10) + (lo - 0xDC00);
                                } else {
                                    pos_ = save; // leave lone surrogate escaped handling below
                                    appendUtf8(out, code);
                                    continue;
                                }
                            }
                        }
                        appendUtf8(out, code);
                        break;
                    }
                    default:
                        throw std::invalid_argument(
                            std::string("JSON parse error: invalid escape \\") + e);
                }
            } else {
                if (static_cast<unsigned char>(c) < 0x20)
                    throw std::invalid_argument(
                        "JSON parse error: unescaped control character in string");
                out.push_back(c);
            }
        }
        return out;
    }

    static void appendUtf8(std::string& out, unsigned code) {
        if (code <= 0x7F) {
            out.push_back(static_cast<char>(code));
        } else if (code <= 0x7FF) {
            out.push_back(static_cast<char>(0xC0 | (code >> 6)));
            out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
        } else if (code <= 0xFFFF) {
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

    JsonValue parseNumber() {
        const size_t start = pos_;
        if (peek() == '-') ++pos_;
        if (pos_ >= text_.size()) fail("malformed number");
        if (text_[pos_] == '0') {
            ++pos_;
        } else if (std::isdigit(static_cast<unsigned char>(text_[pos_]))) {
            while (pos_ < text_.size() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) ++pos_;
        } else {
            fail("malformed number");
        }
        bool hasFraction = false;
        if (pos_ < text_.size() && text_[pos_] == '.') {
            hasFraction = true;
            ++pos_;
            if (pos_ >= text_.size() || !std::isdigit(static_cast<unsigned char>(text_[pos_])))
                fail("malformed number: digits expected after '.'");
            while (pos_ < text_.size() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) ++pos_;
        }
        if (pos_ < text_.size() && (text_[pos_] == 'e' || text_[pos_] == 'E')) {
            ++pos_;
            if (pos_ < text_.size() && (text_[pos_] == '+' || text_[pos_] == '-')) ++pos_;
            if (pos_ >= text_.size() || !std::isdigit(static_cast<unsigned char>(text_[pos_])))
                fail("malformed number: digits expected after exponent");
            while (pos_ < text_.size() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) ++pos_;
        }
        (void)hasFraction;
        return JsonValue::makeNumber(text_.substr(start, pos_ - start));
    }

    JsonValue parseBool() {
        if (text_.compare(pos_, 4, "true") == 0) { pos_ += 4; return JsonValue::makeBool(true); }
        if (text_.compare(pos_, 5, "false") == 0) { pos_ += 5; return JsonValue::makeBool(false); }
        fail("invalid literal");
    }

    JsonValue parseNull() {
        if (text_.compare(pos_, 4, "null") == 0) { pos_ += 4; return JsonValue::makeNull(); }
        fail("invalid literal");
    }
};

} // namespace

JsonValue parseJson(const std::string& text) {
    return Parser(text).parse();
}

JsonValue parseJsonAt(const std::string& text, std::size_t& pos) {
    Parser parser(text);
    return parser.parseOne(pos);
}

std::string jsonEscape(const std::string& raw) {
    std::string out;
    out.reserve(raw.size() + 2);
    for (unsigned char c : raw) {
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
    return out;
}

std::string dumpString(const std::string& raw) {
    return "\"" + jsonEscape(raw) + "\"";
}

std::string dumpInt(long long value) {
    return std::to_string(value);
}

std::string JsonValue::dump() const {
    switch (type) {
        case JsonType::Null: return "null";
        case JsonType::Bool: return boolean ? "true" : "false";
        case JsonType::Number: return number;
        case JsonType::String: return dumpString(str);
        case JsonType::Array: {
            std::string out = "[";
            for (size_t i = 0; i < arr.size(); ++i) {
                if (i) out += ",";
                out += arr[i].dump();
            }
            return out + "]";
        }
        case JsonType::Object: {
            std::string out = "{";
            bool first = true;
            for (const auto& [key, value] : obj) {
                if (!first) out += ",";
                first = false;
                out += dumpString(key) + ":" + value.dump();
            }
            return out + "}";
        }
    }
    return "null";
}

} // namespace dagpaths
