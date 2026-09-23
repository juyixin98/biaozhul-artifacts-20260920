#include "json.hpp"

#include <cmath>
#include <cstdio>
#include <sstream>

namespace json {

const Value* Value::find(const std::string& key) const {
    if (type != Type::Object) return nullptr;
    for (const auto& kv : obj)
        if (kv.first == key) return &kv.second;
    return nullptr;
}

namespace {

struct Parser {
    const std::string& s;
    size_t i = 0;
    std::string err;

    explicit Parser(const std::string& text) : s(text) {}

    void skipWs() {
        while (i < s.size()) {
            char c = s[i];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++i;
            else break;
        }
    }

    bool fail(std::string msg) {
        if (err.empty()) {
            std::ostringstream os;
            os << "JSON parse error at offset " << i << ": " << msg;
            err = os.str();
        }
        return false;
    }

    bool parseValue(Value& out) {
        skipWs();
        if (i >= s.size()) return fail("unexpected end of input");
        char c = s[i];
        switch (c) {
            case '{': return parseObject(out);
            case '[': return parseArray(out);
            case '"': {
                out.type = Type::String;
                return parseString(out.str);
            }
            case 't': return parseLit("true", out, true);
            case 'f': return parseLit("false", out, false);
            case 'n': return parseNullLit(out);
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber(out);
                return fail("unexpected character");
        }
    }

    bool parseLit(const char* lit, Value& out, bool b) {
        size_t n = std::char_traits<char>::length(lit);
        if (s.compare(i, n, lit) != 0) return fail("invalid literal");
        i += n;
        out.type = Type::Bool;
        out.boolean = b;
        return true;
    }

    bool parseNullLit(Value& out) {
        if (s.compare(i, 4, "null") != 0) return fail("invalid literal");
        i += 4;
        out.type = Type::Null;
        return true;
    }

    bool parseNumber(Value& out) {
        size_t start = i;
        if (s[i] == '-') ++i;
        if (i >= s.size()) return fail("bad number");
        if (s[i] == '0') {
            ++i;
        } else if (s[i] >= '1' && s[i] <= '9') {
            while (i < s.size() && s[i] >= '0' && s[i] <= '9') ++i;
        } else {
            return fail("bad number");
        }
        if (i < s.size() && s[i] == '.') {
            ++i;
            if (i >= s.size() || !(s[i] >= '0' && s[i] <= '9'))
                return fail("bad fraction");
            while (i < s.size() && s[i] >= '0' && s[i] <= '9') ++i;
        }
        if (i < s.size() && (s[i] == 'e' || s[i] == 'E')) {
            ++i;
            if (i < s.size() && (s[i] == '+' || s[i] == '-')) ++i;
            if (i >= s.size() || !(s[i] >= '0' && s[i] <= '9'))
                return fail("bad exponent");
            while (i < s.size() && s[i] >= '0' && s[i] <= '9') ++i;
        }
        out.type = Type::Number;
        out.raw = s.substr(start, i - start);
        return true;
    }

    void appendUtf8(std::string& dst, uint32_t cp) {
        if (cp <= 0x7F) {
            dst.push_back(static_cast<char>(cp));
        } else if (cp <= 0x7FF) {
            dst.push_back(static_cast<char>(0xC0 | (cp >> 6)));
            dst.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        } else if (cp <= 0xFFFF) {
            dst.push_back(static_cast<char>(0xE0 | (cp >> 12)));
            dst.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            dst.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        } else {
            dst.push_back(static_cast<char>(0xF0 | (cp >> 18)));
            dst.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
            dst.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            dst.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        }
    }

    bool parseHex4(uint32_t& cp) {
        cp = 0;
        if (i + 4 > s.size()) return fail("bad unicode escape");
        for (int k = 0; k < 4; ++k) {
            char c = s[i++];
            cp <<= 4;
            if (c >= '0' && c <= '9') cp |= static_cast<uint32_t>(c - '0');
            else if (c >= 'a' && c <= 'f') cp |= static_cast<uint32_t>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') cp |= static_cast<uint32_t>(c - 'A' + 10);
            else return fail("bad hex digit");
        }
        return true;
    }

    bool parseString(std::string& out) {
        ++i; // opening quote
        while (i < s.size()) {
            char c = s[i++];
            if (c == '"') return true;
            if (c == '\\') {
                if (i >= s.size()) return fail("bad escape");
                char e = s[i++];
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
                        uint32_t cp;
                        if (!parseHex4(cp)) return false;
                        if (cp >= 0xD800 && cp <= 0xDBFF) {
                            if (i + 2 > s.size() || s[i] != '\\' || s[i + 1] != 'u')
                                return fail("expected low surrogate");
                            i += 2;
                            uint32_t lo;
                            if (!parseHex4(lo)) return false;
                            if (lo < 0xDC00 || lo > 0xDFFF)
                                return fail("bad low surrogate");
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                            return fail("unexpected low surrogate");
                        }
                        appendUtf8(out, cp);
                        break;
                    }
                    default: return fail("bad escape");
                }
            } else if (static_cast<unsigned char>(c) < 0x20) {
                return fail("unescaped control character");
            } else {
                out.push_back(c);
            }
        }
        return fail("unterminated string");
    }

    bool parseArray(Value& out) {
        out.type = Type::Array;
        ++i; // [
        skipWs();
        if (i < s.size() && s[i] == ']') { ++i; return true; }
        while (true) {
            Value v;
            if (!parseValue(v)) return false;
            out.arr.push_back(std::move(v));
            skipWs();
            if (i >= s.size()) return fail("unterminated array");
            if (s[i] == ',') { ++i; continue; }
            if (s[i] == ']') { ++i; return true; }
            return fail("expected ',' or ']'");
        }
    }

    bool parseObject(Value& out) {
        out.type = Type::Object;
        ++i; // {
        skipWs();
        if (i < s.size() && s[i] == '}') { ++i; return true; }
        while (true) {
            skipWs();
            if (i >= s.size() || s[i] != '"') return fail("expected key string");
            std::string key;
            if (!parseString(key)) return false;
            skipWs();
            if (i >= s.size() || s[i] != ':') return fail("expected ':'");
            ++i;
            Value v;
            if (!parseValue(v)) return false;
            out.obj.emplace_back(std::move(key), std::move(v));
            skipWs();
            if (i >= s.size()) return fail("unterminated object");
            if (s[i] == ',') { ++i; continue; }
            if (s[i] == '}') { ++i; return true; }
            return fail("expected ',' or '}'");
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

void dumpInto(const Value& v, std::string& out) {
    switch (v.type) {
        case Type::Null: out += "null"; break;
        case Type::Bool: out += v.boolean ? "true" : "false"; break;
        case Type::Number: out += v.raw.empty() ? "0" : v.raw; break;
        case Type::String: dumpString(v.str, out); break;
        case Type::Array: {
            out.push_back('[');
            for (size_t k = 0; k < v.arr.size(); ++k) {
                if (k) out.push_back(',');
                dumpInto(v.arr[k], out);
            }
            out.push_back(']');
            break;
        }
        case Type::Object: {
            out.push_back('{');
            for (size_t k = 0; k < v.obj.size(); ++k) {
                if (k) out.push_back(',');
                dumpString(v.obj[k].first, out);
                out.push_back(':');
                dumpInto(v.obj[k].second, out);
            }
            out.push_back('}');
            break;
        }
    }
}

void prettyInto(const Value& v, std::string& out, int indent) {
    auto pad = [&](int n) { out.append(static_cast<size_t>(n) * 2, ' '); };
    switch (v.type) {
        case Type::Null: out += "null"; break;
        case Type::Bool: out += v.boolean ? "true" : "false"; break;
        case Type::Number: out += v.raw.empty() ? "0" : v.raw; break;
        case Type::String: dumpString(v.str, out); break;
        case Type::Array: {
            if (v.arr.empty()) { out += "[]"; break; }
            out += "[\n";
            for (size_t k = 0; k < v.arr.size(); ++k) {
                pad(indent + 1);
                prettyInto(v.arr[k], out, indent + 1);
                if (k + 1 < v.arr.size()) out.push_back(',');
                out.push_back('\n');
            }
            pad(indent);
            out.push_back(']');
            break;
        }
        case Type::Object: {
            if (v.obj.empty()) { out += "{}"; break; }
            out += "{\n";
            for (size_t k = 0; k < v.obj.size(); ++k) {
                pad(indent + 1);
                dumpString(v.obj[k].first, out);
                out += ": ";
                prettyInto(v.obj[k].second, out, indent + 1);
                if (k + 1 < v.obj.size()) out.push_back(',');
                out.push_back('\n');
            }
            pad(indent);
            out.push_back('}');
            break;
        }
    }
}

} // namespace

ParseResult parse(const std::string& text) {
    ParseResult r;
    Parser p(text);
    if (!p.parseValue(r.root)) {
        r.ok = false;
        r.error = p.err;
        return r;
    }
    p.skipWs();
    if (p.i != text.size()) {
        r.ok = false;
        r.error = "JSON parse error: trailing characters after value";
        return r;
    }
    r.ok = true;
    return r;
}

Value makeNull() { Value v; v.type = Type::Null; return v; }
Value makeBool(bool b) { Value v; v.type = Type::Bool; v.boolean = b; return v; }
Value makeRawNumber(std::string raw) {
    Value v; v.type = Type::Number; v.raw = std::move(raw); return v;
}
Value makeInt(int64_t n) {
    Value v; v.type = Type::Number; v.raw = std::to_string(n); return v;
}
Value makeString(std::string s) {
    Value v; v.type = Type::String; v.str = std::move(s); return v;
}
Value makeArray() { Value v; v.type = Type::Array; return v; }
Value makeObject() { Value v; v.type = Type::Object; return v; }

void push(Value& arr, Value v) { arr.arr.push_back(std::move(v)); }
void set(Value& obj, std::string key, Value v) {
    obj.obj.emplace_back(std::move(key), std::move(v));
}

std::string dump(const Value& v) {
    std::string out;
    dumpInto(v, out);
    return out;
}

std::string dumpPretty(const Value& v) {
    std::string out;
    prettyInto(v, out, 0);
    out.push_back('\n');
    return out;
}

} // namespace json
