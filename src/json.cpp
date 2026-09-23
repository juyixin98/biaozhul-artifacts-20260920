#include "json.hpp"

#include <cctype>
#include <cstdio>
#include <stdexcept>

JsonPtr JsonValue::makeNull() {
    auto v = std::make_shared<JsonValue>();
    v->type = JsonType::Null;
    return v;
}
JsonPtr JsonValue::makeBool(bool b) {
    auto v = std::make_shared<JsonValue>();
    v->type = JsonType::Bool;
    v->boolean = b;
    return v;
}
JsonPtr JsonValue::makeNumber(std::string token) {
    auto v = std::make_shared<JsonValue>();
    v->type = JsonType::Number;
    v->numToken = std::move(token);
    return v;
}
JsonPtr JsonValue::makeString(std::string s) {
    auto v = std::make_shared<JsonValue>();
    v->type = JsonType::String;
    v->str = std::move(s);
    return v;
}
JsonPtr JsonValue::makeArray() {
    auto v = std::make_shared<JsonValue>();
    v->type = JsonType::Array;
    return v;
}
JsonPtr JsonValue::makeObject() {
    auto v = std::make_shared<JsonValue>();
    v->type = JsonType::Object;
    return v;
}

const JsonPtr* JsonValue::get(const std::string& key) const {
    for (const auto& kv : obj)
        if (kv.first == key) return &kv.second;
    return nullptr;
}

void JsonValue::set(const std::string& key, JsonPtr v) {
    for (auto& kv : obj) {
        if (kv.first == key) {
            kv.second = std::move(v);
            return;
        }
    }
    obj.emplace_back(key, std::move(v));
}

namespace {

struct Parser {
    const std::string& s;
    size_t i = 0;
    explicit Parser(const std::string& text) : s(text) {}

    [[noreturn]] void fail(const std::string& msg) {
        throw std::runtime_error("JSON parse error at offset " +
                                 std::to_string(i) + ": " + msg);
    }

    void skipWs() {
        while (i < s.size()) {
            char c = s[i];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r')
                ++i;
            else
                break;
        }
    }

    JsonPtr parseValue() {
        skipWs();
        if (i >= s.size()) fail("unexpected end of input");
        char c = s[i];
        if (c == '{') return parseObject();
        if (c == '[') return parseArray();
        if (c == '"') return JsonValue::makeString(parseString());
        if (c == 't' || c == 'f') return parseBool();
        if (c == 'n') return parseNull();
        if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
        fail("unexpected character");
    }

    JsonPtr parseObject() {
        auto v = JsonValue::makeObject();
        ++i; // {
        skipWs();
        if (i < s.size() && s[i] == '}') { ++i; return v; }
        while (true) {
            skipWs();
            if (i >= s.size() || s[i] != '"') fail("expected string key");
            std::string key = parseString();
            skipWs();
            if (i >= s.size() || s[i] != ':') fail("expected ':'");
            ++i;
            v->obj.emplace_back(std::move(key), parseValue());
            skipWs();
            if (i >= s.size()) fail("unterminated object");
            if (s[i] == ',') { ++i; continue; }
            if (s[i] == '}') { ++i; break; }
            fail("expected ',' or '}'");
        }
        return v;
    }

    JsonPtr parseArray() {
        auto v = JsonValue::makeArray();
        ++i; // [
        skipWs();
        if (i < s.size() && s[i] == ']') { ++i; return v; }
        while (true) {
            v->arr.push_back(parseValue());
            skipWs();
            if (i >= s.size()) fail("unterminated array");
            if (s[i] == ',') { ++i; continue; }
            if (s[i] == ']') { ++i; break; }
            fail("expected ',' or ']'");
        }
        return v;
    }

    std::string parseString() {
        ++i; // opening quote
        std::string out;
        while (i < s.size()) {
            char c = s[i++];
            if (c == '"') return out;
            if (c == '\\') {
                if (i >= s.size()) fail("bad escape");
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
                        if (i + 4 > s.size()) fail("bad \\u escape");
                        unsigned code = 0;
                        for (int k = 0; k < 4; ++k) {
                            char h = s[i++];
                            code <<= 4;
                            if (h >= '0' && h <= '9') code |= unsigned(h - '0');
                            else if (h >= 'a' && h <= 'f') code |= unsigned(h - 'a' + 10);
                            else if (h >= 'A' && h <= 'F') code |= unsigned(h - 'A' + 10);
                            else fail("bad hex digit in \\u escape");
                        }
                        // Encode as UTF-8. Surrogate pairs are handled below.
                        auto appendUtf8 = [&](unsigned cp) {
                            if (cp < 0x80) {
                                out.push_back(char(cp));
                            } else if (cp < 0x800) {
                                out.push_back(char(0xC0 | (cp >> 6)));
                                out.push_back(char(0x80 | (cp & 0x3F)));
                            } else if (cp < 0x10000) {
                                out.push_back(char(0xE0 | (cp >> 12)));
                                out.push_back(char(0x80 | ((cp >> 6) & 0x3F)));
                                out.push_back(char(0x80 | (cp & 0x3F)));
                            } else {
                                out.push_back(char(0xF0 | (cp >> 18)));
                                out.push_back(char(0x80 | ((cp >> 12) & 0x3F)));
                                out.push_back(char(0x80 | ((cp >> 6) & 0x3F)));
                                out.push_back(char(0x80 | (cp & 0x3F)));
                            }
                        };
                        if (code >= 0xD800 && code <= 0xDBFF) {
                            if (i + 6 <= s.size() && s[i] == '\\' && s[i + 1] == 'u') {
                                i += 2;
                                unsigned lo = 0;
                                for (int k = 0; k < 4; ++k) {
                                    char h = s[i++];
                                    lo <<= 4;
                                    if (h >= '0' && h <= '9') lo |= unsigned(h - '0');
                                    else if (h >= 'a' && h <= 'f') lo |= unsigned(h - 'a' + 10);
                                    else if (h >= 'A' && h <= 'F') lo |= unsigned(h - 'A' + 10);
                                    else fail("bad hex digit in \\u escape");
                                }
                                if (lo >= 0xDC00 && lo <= 0xDFFF) {
                                    unsigned cp = 0x10000 + ((code - 0xD800) << 10) +
                                                  (lo - 0xDC00);
                                    appendUtf8(cp);
                                } else {
                                    fail("bad low surrogate");
                                }
                            } else {
                                fail("expected low surrogate");
                            }
                        } else {
                            appendUtf8(code);
                        }
                        break;
                    }
                    default: fail("bad escape character");
                }
            } else {
                // Copy raw UTF-8 bytes through.
                out.push_back(c);
            }
        }
        fail("unterminated string");
    }

    JsonPtr parseBool() {
        if (s.compare(i, 4, "true") == 0) { i += 4; return JsonValue::makeBool(true); }
        if (s.compare(i, 5, "false") == 0) { i += 5; return JsonValue::makeBool(false); }
        fail("invalid literal");
    }

    JsonPtr parseNull() {
        if (s.compare(i, 4, "null") == 0) { i += 4; return JsonValue::makeNull(); }
        fail("invalid literal");
    }

    JsonPtr parseNumber() {
        size_t start = i;
        if (s[i] == '-') ++i;
        if (i >= s.size()) fail("bad number");
        if (s[i] == '0') {
            ++i;
        } else if (s[i] >= '1' && s[i] <= '9') {
            while (i < s.size() && std::isdigit(static_cast<unsigned char>(s[i]))) ++i;
        } else {
            fail("bad number");
        }
        if (i < s.size() && s[i] == '.') {
            ++i;
            if (i >= s.size() || !std::isdigit(static_cast<unsigned char>(s[i])))
                fail("bad fraction");
            while (i < s.size() && std::isdigit(static_cast<unsigned char>(s[i]))) ++i;
        }
        if (i < s.size() && (s[i] == 'e' || s[i] == 'E')) {
            ++i;
            if (i < s.size() && (s[i] == '+' || s[i] == '-')) ++i;
            if (i >= s.size() || !std::isdigit(static_cast<unsigned char>(s[i])))
                fail("bad exponent");
            while (i < s.size() && std::isdigit(static_cast<unsigned char>(s[i]))) ++i;
        }
        return JsonValue::makeNumber(s.substr(start, i - start));
    }
};

void dumpTo(const JsonValue& v, std::string& out) {
    switch (v.type) {
        case JsonType::Null: out += "null"; break;
        case JsonType::Bool: out += v.boolean ? "true" : "false"; break;
        case JsonType::Number: out += v.numToken.empty() ? "0" : v.numToken; break;
        case JsonType::String: {
            out.push_back('"');
            for (unsigned char c : v.str) {
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
                            out.push_back(char(c));
                        }
                }
            }
            out.push_back('"');
            break;
        }
        case JsonType::Array: {
            out.push_back('[');
            for (size_t k = 0; k < v.arr.size(); ++k) {
                if (k) out.push_back(',');
                dumpTo(*v.arr[k], out);
            }
            out.push_back(']');
            break;
        }
        case JsonType::Object: {
            out.push_back('{');
            for (size_t k = 0; k < v.obj.size(); ++k) {
                if (k) out.push_back(',');
                auto keyNode = JsonValue::makeString(v.obj[k].first);
                dumpTo(*keyNode, out);
                out.push_back(':');
                dumpTo(*v.obj[k].second, out);
            }
            out.push_back('}');
            break;
        }
    }
}

void dumpPrettyTo(const JsonValue& v, std::string& out, int indent, int depth) {
    auto pad = [&](int d) { out.append(static_cast<size_t>(indent * d), ' '); };
    switch (v.type) {
        case JsonType::Null: out += "null"; break;
        case JsonType::Bool: out += v.boolean ? "true" : "false"; break;
        case JsonType::Number: out += v.numToken.empty() ? "0" : v.numToken; break;
        case JsonType::String: {
            JsonValue s = v;
            std::string compact;
            dumpTo(s, compact);
            out += compact;
            break;
        }
        case JsonType::Array: {
            if (v.arr.empty()) { out += "[]"; break; }
            out += "[\n";
            for (size_t k = 0; k < v.arr.size(); ++k) {
                pad(depth + 1);
                dumpPrettyTo(*v.arr[k], out, indent, depth + 1);
                if (k + 1 < v.arr.size()) out.push_back(',');
                out.push_back('\n');
            }
            pad(depth);
            out.push_back(']');
            break;
        }
        case JsonType::Object: {
            if (v.obj.empty()) { out += "{}"; break; }
            out += "{\n";
            for (size_t k = 0; k < v.obj.size(); ++k) {
                pad(depth + 1);
                auto keyNode = JsonValue::makeString(v.obj[k].first);
                std::string compact;
                dumpTo(*keyNode, compact);
                out += compact;
                out += ": ";
                dumpPrettyTo(*v.obj[k].second, out, indent, depth + 1);
                if (k + 1 < v.obj.size()) out.push_back(',');
                out.push_back('\n');
            }
            pad(depth);
            out.push_back('}');
            break;
        }
    }
}

} // namespace

JsonPtr jsonParse(const std::string& text) {
    Parser p(text);
    JsonPtr v = p.parseValue();
    p.skipWs();
    if (p.i != text.size()) p.fail("trailing characters");
    return v;
}

std::string jsonDump(const JsonValue& v) {
    std::string out;
    dumpTo(v, out);
    return out;
}

std::string jsonDumpPretty(const JsonValue& v) {
    std::string out;
    dumpPrettyTo(v, out, 2, 0);
    out.push_back('\n');
    return out;
}
