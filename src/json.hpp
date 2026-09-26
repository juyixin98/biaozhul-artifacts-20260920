// Minimal hand-written JSON parser / serializer (no external dependencies).
#pragma once

#include <string>
#include <string_view>
#include <vector>
#include <utility>

namespace tdw {

class Json {
public:
    enum Type { NUL, BOOL, NUM, STR, ARR, OBJ } type = NUL;
    bool boolean = false;
    double number = 0.0;
    std::string text;
    std::vector<Json> items;
    std::vector<std::pair<std::string, Json>> members;

    // Parses a complete JSON document. Throws std::runtime_error on any
    // syntax error or trailing garbage.
    static Json parse(std::string_view src);

    // Pretty-printed serialization. indent <= 0 produces compact output.
    std::string dump(int indent = 2) const;

    const Json* find(std::string_view key) const;
    bool isIntegral(long long& out) const;
};

} // namespace tdw
