#pragma once
// Minimal JSON parser sufficient for the request/response format of this
// project. Supports objects, arrays, strings (escapes + \uXXXX incl. surrogate
// pairs), numbers, booleans and null. No external dependencies.
#include <cerrno>
#include <cstdlib>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace minijson {

struct Value {
    enum class Type { Null, Bool, Int, Double, String, Array, Object };

    Type type = Type::Null;
    bool boolean = false;
    long long integer = 0;
    double number = 0.0;
    std::string str;
    std::vector<Value> arr;
    std::vector<std::pair<std::string, Value>> obj;

    const Value* find(const std::string& key) const {
        if (type != Type::Object) return nullptr;
        for (const auto& kv : obj) {
            if (kv.first == key) return &kv.second;
        }
        return nullptr;
    }
};

class Parser {
public:
    explicit Parser(const std::string& text) : s_(text) {}

    Value parse() {
        skipWs();
        Value v = parseValue(0);
        skipWs();
        if (pos_ != s_.size()) fail("trailing characters after top-level value");
        return v;
    }

private:
    static constexpr int kMaxDepth = 200;

    const std::string& s_;
    std::size_t pos_ = 0;

    [[noreturn]] void fail(const std::string& msg) const {
        throw std::runtime_error("JSON parse error at byte " + std::to_string(pos_) + ": " + msg);
    }

    char peek() const { return pos_ < s_.size() ? s_[pos_] : '\0'; }

    char get() { return pos_ < s_.size() ? s_[pos_++] : '\0'; }

    void skipWs() {
        while (pos_ < s_.size() &&
               (s_[pos_] == ' ' || s_[pos_] == '\t' || s_[pos_] == '\n' || s_[pos_] == '\r'))
            ++pos_;
    }

    void expect(char c) {
        if (get() != c) fail(std::string("expected '") + c + "'");
    }

    void expectLiteral(const char* lit) {
        for (const char* p = lit; *p; ++p) {
            if (get() != *p) fail(std::string("invalid literal, expected ") + lit);
        }
    }

    Value parseValue(int depth) {
        if (depth > kMaxDepth) fail("nesting too deep");
        switch (peek()) {
            case '{': return parseObject(depth);
            case '[': return parseArray(depth);
            case '"': {
                Value v;
                v.type = Value::Type::String;
                v.str = parseString();
                return v;
            }
            case 't': {
                expectLiteral("true");
                Value v;
                v.type = Value::Type::Bool;
                v.boolean = true;
                return v;
            }
            case 'f': {
                expectLiteral("false");
                Value v;
                v.type = Value::Type::Bool;
                v.boolean = false;
                return v;
            }
            case 'n': {
                expectLiteral("null");
                return Value{};
            }
            default: return parseNumber();
        }
    }

    Value parseObject(int depth) {
        expect('{');
        Value v;
        v.type = Value::Type::Object;
        skipWs();
        if (peek() == '}') {
            get();
            return v;
        }
        while (true) {
            skipWs();
            if (peek() != '"') fail("object key must be a string");
            std::string key = parseString();
            skipWs();
            expect(':');
            skipWs();
            v.obj.emplace_back(std::move(key), parseValue(depth + 1));
            skipWs();
            char c = get();
            if (c == '}') break;
            if (c != ',') fail("expected ',' or '}' in object");
        }
        return v;
    }

    Value parseArray(int depth) {
        expect('[');
        Value v;
        v.type = Value::Type::Array;
        skipWs();
        if (peek() == ']') {
            get();
            return v;
        }
        while (true) {
            skipWs();
            v.arr.push_back(parseValue(depth + 1));
            skipWs();
            char c = get();
            if (c == ']') break;
            if (c != ',') fail("expected ',' or ']' in array");
        }
        return v;
    }

    unsigned parseHex4() {
        unsigned cp = 0;
        for (int i = 0; i < 4; ++i) {
            char c = get();
            cp <<= 4;
            if (c >= '0' && c <= '9') cp += (unsigned)(c - '0');
            else if (c >= 'a' && c <= 'f') cp += (unsigned)(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') cp += (unsigned)(c - 'A' + 10);
            else fail("invalid \\u escape");
        }
        return cp;
    }

    static void appendUtf8(std::string& out, unsigned cp) {
        if (cp < 0x80) {
            out += (char)cp;
        } else if (cp < 0x800) {
            out += (char)(0xC0 | (cp >> 6));
            out += (char)(0x80 | (cp & 0x3F));
        } else if (cp < 0x10000) {
            out += (char)(0xE0 | (cp >> 12));
            out += (char)(0x80 | ((cp >> 6) & 0x3F));
            out += (char)(0x80 | (cp & 0x3F));
        } else {
            out += (char)(0xF0 | (cp >> 18));
            out += (char)(0x80 | ((cp >> 12) & 0x3F));
            out += (char)(0x80 | ((cp >> 6) & 0x3F));
            out += (char)(0x80 | (cp & 0x3F));
        }
    }

    std::string parseString() {
        expect('"');
        std::string out;
        while (true) {
            if (pos_ >= s_.size()) fail("unterminated string");
            char c = get();
            if (c == '"') break;
            if (c == '\\') {
                char e = get();
                switch (e) {
                    case '"': out += '"'; break;
                    case '\\': out += '\\'; break;
                    case '/': out += '/'; break;
                    case 'b': out += '\b'; break;
                    case 'f': out += '\f'; break;
                    case 'n': out += '\n'; break;
                    case 'r': out += '\r'; break;
                    case 't': out += '\t'; break;
                    case 'u': {
                        unsigned cp = parseHex4();
                        if (cp >= 0xD800 && cp <= 0xDBFF) {
                            if (get() != '\\' || get() != 'u')
                                fail("lone high surrogate in \\u escape");
                            unsigned lo = parseHex4();
                            if (lo < 0xDC00 || lo > 0xDFFF)
                                fail("invalid low surrogate in \\u escape");
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                            fail("lone low surrogate in \\u escape");
                        }
                        appendUtf8(out, cp);
                        break;
                    }
                    default: fail("invalid escape sequence");
                }
            } else if ((unsigned char)c < 0x20) {
                fail("unescaped control character in string");
            } else {
                out += c;
            }
        }
        return out;
    }

    Value parseNumber() {
        std::size_t start = pos_;
        if (peek() == '-') ++pos_;
        bool is_double = false;
        while (pos_ < s_.size()) {
            char c = s_[pos_];
            if (c >= '0' && c <= '9') {
                ++pos_;
            } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                if (c == '.' || c == 'e' || c == 'E') is_double = true;
                ++pos_;
            } else {
                break;
            }
        }
        if (pos_ == start || (pos_ == start + 1 && s_[start] == '-'))
            fail("invalid number");
        std::string tok = s_.substr(start, pos_ - start);
        Value v;
        if (is_double) {
            errno = 0;
            v.number = std::strtod(tok.c_str(), nullptr);
            if (errno == ERANGE) fail("number out of range");
            v.type = Value::Type::Double;
        } else {
            errno = 0;
            v.integer = std::strtoll(tok.c_str(), nullptr, 10);
            if (errno == ERANGE) fail("integer out of range");
            v.type = Value::Type::Int;
        }
        return v;
    }
};

} // namespace minijson
