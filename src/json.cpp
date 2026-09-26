#include "json.hpp"

#include <cctype>

const Json* Json::find(const std::string& key) const {
    if (type_ != Type::Object) return nullptr;
    auto it = obj_.find(key);
    return it == obj_.end() ? nullptr : &it->second;
}

namespace {

class Parser {
public:
    explicit Parser(const std::string& text) : text_(text) {}

    Json run() {
        skipWs();
        Json v = parseValue();
        skipWs();
        if (pos_ != text_.size()) {
            fail("unexpected trailing characters");
        }
        return v;
    }

private:
    const std::string& text_;
    size_t pos_ = 0;

    [[noreturn]] void fail(const std::string& msg) const {
        throw JsonError("JSON parse error at byte " + std::to_string(pos_) + ": " + msg);
    }

    void skipWs() {
        while (pos_ < text_.size() &&
               (text_[pos_] == ' ' || text_[pos_] == '\t' ||
                text_[pos_] == '\n' || text_[pos_] == '\r')) {
            ++pos_;
        }
    }

    char peek() const { return pos_ < text_.size() ? text_[pos_] : '\0'; }

    void expect(char c) {
        if (peek() != c) fail(std::string("expected '") + c + "'");
        ++pos_;
    }

    bool consume(char c) {
        if (peek() == c) {
            ++pos_;
            return true;
        }
        return false;
    }

    Json parseValue() {
        switch (peek()) {
            case '{': return parseObject();
            case '[': return parseArray();
            case '"': return Json(parseString());
            case 't': return parseLiteral("true", Json(true));
            case 'f': return parseLiteral("false", Json(false));
            case 'n': return parseLiteral("null", Json());
            case '-':
            case '0': case '1': case '2': case '3': case '4':
            case '5': case '6': case '7': case '8': case '9':
                return parseNumber();
            default:
                fail("unexpected character");
        }
    }

    Json parseLiteral(const char* word, Json value) {
        for (const char* p = word; *p; ++p) {
            if (peek() != *p) fail("invalid literal");
            ++pos_;
        }
        return value;
    }

    Json parseNumber() {
        size_t start = pos_;
        consume('-');
        if (!std::isdigit(static_cast<unsigned char>(peek()))) {
            fail("invalid number");
        }
        while (std::isdigit(static_cast<unsigned char>(peek()))) ++pos_;
        if (peek() == '.' || peek() == 'e' || peek() == 'E') {
            fail("only integer numbers are supported");
        }
        try {
            return Json(static_cast<int64_t>(
                std::stoll(text_.substr(start, pos_ - start))));
        } catch (const std::out_of_range&) {
            fail("integer out of 64-bit range (use a decimal string for large values)");
        }
    }

    std::string parseString() {
        expect('"');
        std::string out;
        while (true) {
            char c = peek();
            if (c == '\0') fail("unterminated string");
            ++pos_;
            if (c == '"') return out;
            if (c == '\\') {
                char esc = peek();
                ++pos_;
                switch (esc) {
                    case '"': out += '"'; break;
                    case '\\': out += '\\'; break;
                    case '/': out += '/'; break;
                    case 'b': out += '\b'; break;
                    case 'f': out += '\f'; break;
                    case 'n': out += '\n'; break;
                    case 'r': out += '\r'; break;
                    case 't': out += '\t'; break;
                    case 'u': out += parseUnicodeEscape(); break;
                    default: fail("invalid escape sequence");
                }
            } else {
                out += c;
            }
        }
    }

    // Decodes \uXXXX (including surrogate pairs) to UTF-8.
    std::string parseUnicodeEscape() {
        uint32_t cp = parseHex4();
        if (cp >= 0xD800 && cp <= 0xDBFF) {
            if (peek() == '\\') {
                ++pos_;
                if (peek() == 'u') {
                    ++pos_;
                    uint32_t lo = parseHex4();
                    if (lo >= 0xDC00 && lo <= 0xDFFF) {
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                    } else {
                        fail("invalid low surrogate");
                    }
                } else {
                    fail("unpaired high surrogate");
                }
            } else {
                fail("unpaired high surrogate");
            }
        }
        std::string out;
        if (cp < 0x80) {
            out += static_cast<char>(cp);
        } else if (cp < 0x800) {
            out += static_cast<char>(0xC0 | (cp >> 6));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        } else if (cp < 0x10000) {
            out += static_cast<char>(0xE0 | (cp >> 12));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        } else {
            out += static_cast<char>(0xF0 | (cp >> 18));
            out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        }
        return out;
    }

    uint32_t parseHex4() {
        uint32_t v = 0;
        for (int i = 0; i < 4; ++i) {
            char c = peek();
            ++pos_;
            v <<= 4;
            if (c >= '0' && c <= '9') v |= static_cast<uint32_t>(c - '0');
            else if (c >= 'a' && c <= 'f') v |= static_cast<uint32_t>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') v |= static_cast<uint32_t>(c - 'A' + 10);
            else fail("invalid \\u escape");
        }
        return v;
    }

    Json parseArray() {
        expect('[');
        std::vector<Json> items;
        skipWs();
        if (consume(']')) return Json(std::move(items));
        while (true) {
            skipWs();
            items.push_back(parseValue());
            skipWs();
            if (consume(']')) return Json(std::move(items));
            expect(',');
        }
    }

    Json parseObject() {
        expect('{');
        std::map<std::string, Json> members;
        skipWs();
        if (consume('}')) return Json(std::move(members));
        while (true) {
            skipWs();
            if (peek() != '"') fail("expected object key string");
            std::string key = parseString();
            skipWs();
            expect(':');
            skipWs();
            if (!members.emplace(std::move(key), parseValue()).second) {
                fail("duplicate object key");
            }
            skipWs();
            if (consume('}')) return Json(std::move(members));
            expect(',');
        }
    }
};

void dumpEscaped(const std::string& s, std::string& out) {
    out += '"';
    for (char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += c;
                }
        }
    }
    out += '"';
}

}  // namespace

Json Json::parse(const std::string& text) {
    return Parser(text).run();
}

std::string Json::dump() const {
    std::string out;
    switch (type_) {
        case Type::Null: out = "null"; break;
        case Type::Bool: out = bool_ ? "true" : "false"; break;
        case Type::Int: out = std::to_string(int_); break;
        case Type::String: dumpEscaped(str_, out); break;
        case Type::Array: {
            out = "[";
            for (size_t i = 0; i < arr_.size(); ++i) {
                if (i) out += ',';
                out += arr_[i].dump();
            }
            out += ']';
            break;
        }
        case Type::Object: {
            out = "{";
            bool first = true;
            for (const auto& [k, v] : obj_) {
                if (!first) out += ',';
                first = false;
                dumpEscaped(k, out);
                out += ':';
                out += v.dump();
            }
            out += '}';
            break;
        }
    }
    return out;
}
