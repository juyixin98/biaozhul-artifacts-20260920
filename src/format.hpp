// Shortest round-trip double formatting, matching Python repr / JSON number
// output. std::to_chars with the default general format emits the shortest
// string that parses back to the same IEEE-754 value, which is exactly what
// canonical cross-language digests need.
#pragma once

#include <array>
#include <charconv>
#include <string>

namespace pf {

inline std::string formatDouble(double v) {
    // Normalize negative zero so it matches Python's repr(-0.0) handling in
    // our canonical forms (we always emit "0.0").
    if (v == 0.0) return std::string("0.0");
    std::array<char, 32> buf{};
    auto res = std::to_chars(buf.data(), buf.data() + buf.size(), v);
    std::string s(buf.data(), res.ptr);
    // Python repr always keeps a decimal point (1.0 not 1); match that.
    if (s.find_first_of(".eEnN") == std::string::npos) s += ".0";
    return s;
}

}  // namespace pf
