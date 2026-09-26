#include "json.hpp"

#include <cctype>
#include <cmath>
#include <cstdio>
#include <stdexcept>

namespace tdw {

namespace {

class Parser {
public:
    explicit Parser(std::string_view src) : s_(src) {}

    Json parse() {
        skipWs();
        Json v = parseValue();
        skipWs();
        if (pos_ != s_.size()) fail("trailing characters after JSON value");
        return v;
    }

private:
    // Bounds recursive descent so attacker-controlled deeply nested input
    // ([[[...]...]] or {"a":{"a":...}}) cannot exhaust the stack. Well above
    // any legitimate request in this backend.
    static constexpr int MAX_DEPTH = 256;

    std::string_view s_;
    size_t pos_ = 0;
    int depth_ = 0;

    struct DepthGuard {
        int& depth;
        explicit DepthGuard(int& d, Parser& p) : depth(d) {
            if (++depth > MAX_DEPTH) p.fail("nesting depth exceeds limit of 256");
        }
        ~DepthGuard() { --depth; }
    };

    [[noreturn]] void fail(const std::string& msg) {
        throw std::runtime_error("JSON parse error at offset " +
                                 std::to_string(pos_) + ": " + msg);
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

    char peek() {
        if (pos_ >= s_.size()) fail("unexpected end of input");
        return s_[pos_];
    }

    char next() {
        if (pos_ >= s_.size()) fail("unexpected end of input");
        return s_[pos_++];
    }

    void expect(char c) {
        if (pos_ >= s_.size() || s_[pos_] != c) {
            fail(std::string("expected '") + c + "'");
        }
        ++pos_;
    }

    Json parseValue() {
        skipWs();
        if (pos_ >= s_.size()) fail("unexpected end of input");
        char c = s_[pos_];
        switch (c) {
            case '{': return parseObject();
            case '[': return parseArray();
            case '"': { Json j; j.type = Json::STR; j.text = parseString(); return j; }
            case 't':
            case 'f': return parseBool();
            case 'n': return parseNull();
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
                fail("unexpected character");
        }
    }

    Json parseObject() {
        DepthGuard guard(depth_, *this);
        Json j;
        j.type = Json::OBJ;
        expect('{');
        skipWs();
        if (peek() == '}') { ++pos_; return j; }
        while (true) {
            skipWs();
            if (peek() != '"') fail("expected string key");
            std::string key = parseString();
            skipWs();
            expect(':');
            j.members.emplace_back(std::move(key), parseValue());
            skipWs();
            char c = next();
            if (c == ',') continue;
            if (c == '}') break;
            fail("expected ',' or '}'");
        }
        return j;
    }

    Json parseArray() {
        DepthGuard guard(depth_, *this);
        Json j;
        j.type = Json::ARR;
        expect('[');
        skipWs();
        if (peek() == ']') { ++pos_; return j; }
        while (true) {
            j.items.push_back(parseValue());
            skipWs();
            char c = next();
            if (c == ',') continue;
            if (c == ']') break;
            fail("expected ',' or ']'");
        }
        return j;
    }

    std::string parseString() {
        expect('"');
        std::string out;
        while (true) {
            char c = next();
            if (c == '"') break;
            if (c == '\\') {
                char e = next();
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
                        unsigned code = 0;
                        for (int k = 0; k < 4; ++k) {
                            char h = next();
                            code <<= 4;
                            if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
                            else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
                            else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
                            else fail("invalid unicode escape");
                        }
                        // Encode as UTF-8 (surrogate pairs are not expected in
                        // graph inputs, but handle a lone code point).
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
                        break;
                    }
                    default: fail("invalid escape sequence");
                }
            } else {
                if (static_cast<unsigned char>(c) < 0x20) fail("control character in string");
                out.push_back(c);
            }
        }
        return out;
    }

    Json parseBool() {
        if (s_.compare(pos_, 4, "true") == 0) { pos_ += 4; Json j; j.type = Json::BOOL; j.boolean = true; return j; }
        if (s_.compare(pos_, 5, "false") == 0) { pos_ += 5; Json j; j.type = Json::BOOL; j.boolean = false; return j; }
        fail("invalid literal");
    }

    Json parseNull() {
        if (s_.compare(pos_, 4, "null") == 0) { pos_ += 4; return Json{}; }
        fail("invalid literal");
    }

    Json parseNumber() {
        size_t start = pos_;
        if (peek() == '-') ++pos_;
        while (pos_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[pos_]))) ++pos_;
        if (pos_ < s_.size() && s_[pos_] == '.') {
            ++pos_;
            while (pos_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[pos_]))) ++pos_;
        }
        if (pos_ < s_.size() && (s_[pos_] == 'e' || s_[pos_] == 'E')) {
            ++pos_;
            if (pos_ < s_.size() && (s_[pos_] == '+' || s_[pos_] == '-')) ++pos_;
            while (pos_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[pos_]))) ++pos_;
        }
        Json j;
        j.type = Json::NUM;
        try {
            j.number = std::stod(std::string(s_.substr(start, pos_ - start)));
        } catch (...) {
            fail("invalid number");
        }
        return j;
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
    if (std::floor(d) == d && std::isfinite(d) &&
        std::fabs(d) < 9.007199254740992e15) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "%lld", static_cast<long long>(d));
        out += buf;
    } else {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "%.17g", d);
        out += buf;
    }
}

} // namespace

Json Json::parse(std::string_view src) {
    Parser p(src);
    return p.parse();
}

void dumpInto(const Json& j, std::string& out, int indent, int depth) {
    auto pad = [&](int d) {
        if (indent > 0) {
            out.push_back('\n');
            out.append(static_cast<size_t>(indent) * d, ' ');
        }
    };
    switch (j.type) {
        case Json::NUL: out += "null"; break;
        case Json::BOOL: out += j.boolean ? "true" : "false"; break;
        case Json::NUM: dumpNumber(j.number, out); break;
        case Json::STR: dumpString(j.text, out); break;
        case Json::ARR: {
            if (j.items.empty()) { out += "[]"; break; }
            out.push_back('[');
            for (size_t i = 0; i < j.items.size(); ++i) {
                if (i) out.push_back(',');
                pad(depth + 1);
                dumpInto(j.items[i], out, indent, depth + 1);
            }
            pad(depth);
            out.push_back(']');
            break;
        }
        case Json::OBJ: {
            if (j.members.empty()) { out += "{}"; break; }
            out.push_back('{');
            for (size_t i = 0; i < j.members.size(); ++i) {
                if (i) out.push_back(',');
                pad(depth + 1);
                dumpString(j.members[i].first, out);
                out.push_back(':');
                if (indent > 0) out.push_back(' ');
                dumpInto(j.members[i].second, out, indent, depth + 1);
            }
            pad(depth);
            out.push_back('}');
            break;
        }
    }
}

std::string Json::dump(int indent) const {
    std::string out;
    dumpInto(*this, out, indent, 0);
    return out;
}

const Json* Json::find(std::string_view key) const {
    if (type != OBJ) return nullptr;
    for (const auto& m : members) {
        if (m.first == key) return &m.second;
    }
    return nullptr;
}

bool Json::isIntegral(long long& out) const {
    if (type != NUM) return false;
    if (!std::isfinite(number) || std::floor(number) != number) return false;
    out = static_cast<long long>(number);
    return true;
}

} // namespace tdw
