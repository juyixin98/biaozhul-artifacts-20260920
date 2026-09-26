#include "json.hpp"

#include <cmath>
#include <cctype>
#include <cstdio>
#include <sstream>

namespace minijson {

const Value* Value::find(const std::string& key) const {
    for (const auto& kv : obj)
        if (kv.first == key) return &kv.second;
    return nullptr;
}

Value* Value::find(const std::string& key) {
    for (auto& kv : obj)
        if (kv.first == key) return &kv.second;
    return nullptr;
}

void Value::set(const std::string& key, Value v) {
    for (auto& kv : obj) {
        if (kv.first == key) { kv.second = std::move(v); return; }
    }
    obj.emplace_back(key, std::move(v));
}

namespace {

void dumpString(std::string& out, const std::string& s) {
    out += '"';
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
                    out += static_cast<char>(c);
                }
        }
    }
    out += '"';
}

void dumpInto(const Value& v, std::string& out, int indent, int depth) {
    std::string pad = indent > 0 ? std::string(depth * indent, ' ') : "";
    std::string childPad = indent > 0 ? std::string((depth + 1) * indent, ' ') : "";
    std::string nl = indent > 0 ? "\n" : "";
    switch (v.type) {
        case Value::NUL: out += "null"; break;
        case Value::BOOL: out += v.b ? "true" : "false"; break;
        case Value::INT: out += std::to_string(v.i); break;
        case Value::NUM:
            if (std::isfinite(v.d)) {
                std::ostringstream ss;
                ss << v.d;
                out += ss.str();
            } else {
                out += "null";
            }
            break;
        case Value::STR: dumpString(out, v.s); break;
        case Value::ARR:
            if (v.arr.empty()) { out += "[]"; break; }
            out += '['; out += nl;
            for (size_t k = 0; k < v.arr.size(); ++k) {
                out += childPad;
                dumpInto(v.arr[k], out, indent, depth + 1);
                if (k + 1 < v.arr.size()) out += ',';
                out += nl;
            }
            out += pad; out += ']';
            break;
        case Value::OBJ:
            if (v.obj.empty()) { out += "{}"; break; }
            out += '{'; out += nl;
            for (size_t k = 0; k < v.obj.size(); ++k) {
                out += childPad;
                dumpString(out, v.obj[k].first);
                out += indent > 0 ? ": " : ":";
                dumpInto(v.obj[k].second, out, indent, depth + 1);
                if (k + 1 < v.obj.size()) out += ',';
                out += nl;
            }
            out += pad; out += '}';
            break;
    }
}

class Parser {
public:
    Parser(const std::string& text) : t_(text) {}

    bool run(Value& out, std::string& error) {
        skipWs();
        if (!parseValue(out)) {
            error = err_.empty() ? "invalid JSON" : err_;
            return false;
        }
        skipWs();
        if (pos_ != t_.size()) {
            error = "trailing characters after JSON value at position " + std::to_string(pos_);
            return false;
        }
        return true;
    }

private:
    const std::string& t_;
    size_t pos_ = 0;
    std::string err_;

    bool eof() const { return pos_ >= t_.size(); }
    char peek() const { return t_[pos_]; }

    void skipWs() {
        while (!eof()) {
            char c = peek();
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos_;
            else break;
        }
    }

    bool fail(const std::string& msg) {
        if (err_.empty()) err_ = msg + " at position " + std::to_string(pos_);
        return false;
    }

    bool consume(char c) {
        if (!eof() && peek() == c) { ++pos_; return true; }
        return false;
    }

    bool parseValue(Value& v) {
        skipWs();
        if (eof()) return fail("unexpected end of JSON");
        char c = peek();
        switch (c) {
            case '{': return parseObject(v);
            case '[': return parseArray(v);
            case '"': {
                std::string str;
                if (!parseString(str)) return false;
                v = Value::makeStr(std::move(str));
                return true;
            }
            case 't': case 'f': return parseBool(v);
            case 'n': return parseNull(v);
            default: return parseNumber(v);
        }
    }

    bool parseObject(Value& v) {
        v = Value::makeObj();
        ++pos_;  // '{'
        skipWs();
        if (consume('}')) return true;
        while (true) {
            skipWs();
            if (eof() || peek() != '"') return fail("expected string key");
            std::string key;
            if (!parseString(key)) return false;
            skipWs();
            if (!consume(':')) return fail("expected ':' after key");
            Value val;
            if (!parseValue(val)) return false;
            v.obj.emplace_back(std::move(key), std::move(val));
            skipWs();
            if (consume(',')) continue;
            if (consume('}')) return true;
            return fail("expected ',' or '}' in object");
        }
    }

    bool parseArray(Value& v) {
        v = Value::makeArr();
        ++pos_;  // '['
        skipWs();
        if (consume(']')) return true;
        while (true) {
            Value item;
            if (!parseValue(item)) return false;
            v.arr.push_back(std::move(item));
            skipWs();
            if (consume(',')) continue;
            if (consume(']')) return true;
            return fail("expected ',' or ']' in array");
        }
    }

    bool parseString(std::string& out) {
        ++pos_;  // opening quote
        while (true) {
            if (eof()) return fail("unterminated string");
            char c = t_[pos_++];
            if (c == '"') return true;
            if (c == '\\') {
                if (eof()) return fail("unterminated escape");
                char e = t_[pos_++];
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
                        if (pos_ + 4 > t_.size()) return fail("bad \\u escape");
                        unsigned code = 0;
                        for (int k = 0; k < 4; ++k) {
                            char h = t_[pos_++];
                            code <<= 4;
                            if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
                            else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
                            else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
                            else return fail("bad hex digit in \\u escape");
                        }
                        appendUtf8(out, code);
                        break;
                    }
                    default: return fail("invalid escape character");
                }
            } else if (static_cast<unsigned char>(c) < 0x20) {
                return fail("unescaped control character in string");
            } else {
                out += c;
            }
        }
    }

    static void appendUtf8(std::string& out, unsigned code) {
        if (code < 0x80) {
            out += static_cast<char>(code);
        } else if (code < 0x800) {
            out += static_cast<char>(0xC0 | (code >> 6));
            out += static_cast<char>(0x80 | (code & 0x3F));
        } else {
            out += static_cast<char>(0xE0 | (code >> 12));
            out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (code & 0x3F));
        }
    }

    bool parseBool(Value& v) {
        if (t_.compare(pos_, 4, "true") == 0) { pos_ += 4; v = Value::makeBool(true); return true; }
        if (t_.compare(pos_, 5, "false") == 0) { pos_ += 5; v = Value::makeBool(false); return true; }
        return fail("invalid literal");
    }

    bool parseNull(Value& v) {
        if (t_.compare(pos_, 4, "null") == 0) { pos_ += 4; v = Value::makeNull(); return true; }
        return fail("invalid literal");
    }

    bool parseNumber(Value& v) {
        size_t start = pos_;
        if (peek() == '-') ++pos_;
        bool anyDigit = false;
        while (!eof() && std::isdigit(static_cast<unsigned char>(peek()))) { ++pos_; anyDigit = true; }
        if (!anyDigit) return fail("invalid number");
        bool isReal = false;
        if (!eof() && peek() == '.') {
            isReal = true;
            ++pos_;
            bool frac = false;
            while (!eof() && std::isdigit(static_cast<unsigned char>(peek()))) { ++pos_; frac = true; }
            if (!frac) return fail("invalid number: digits expected after '.'");
        }
        if (!eof() && (peek() == 'e' || peek() == 'E')) {
            isReal = true;
            ++pos_;
            if (!eof() && (peek() == '+' || peek() == '-')) ++pos_;
            bool exp = false;
            while (!eof() && std::isdigit(static_cast<unsigned char>(peek()))) { ++pos_; exp = true; }
            if (!exp) return fail("invalid number: digits expected in exponent");
        }
        std::string num = t_.substr(start, pos_ - start);
        try {
            if (isReal) {
                v = Value::makeNum(std::stod(num));
            } else {
                v = Value::makeInt(std::stoll(num));
            }
        } catch (...) {
            return fail("number out of range");
        }
        return true;
    }
};

}  // namespace

std::string dump(const Value& v, int indent) {
    std::string out;
    dumpInto(v, out, indent, 0);
    return out;
}

bool parse(const std::string& text, Value& out, std::string& error) {
    Parser parser(text);
    return parser.run(out, error);
}

}  // namespace minijson
