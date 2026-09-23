#include "decimal.hpp"

#include <cctype>

namespace decimal {

using i128 = __int128_t;

namespace {

bool parseParts(const std::string& raw, bool& negative,
                std::string& digits, int& fracLen, int& exp) {
    size_t i = 0;
    negative = false;
    fracLen = 0;
    exp = 0;
    digits.clear();
    if (raw.empty()) return false;

    if (raw[i] == '-') { negative = true; ++i; }
    else if (raw[i] == '+') return false; // JSON forbids explicit +
    if (i >= raw.size()) return false;

    if (raw[i] == '0') {
        ++i;
    } else if (raw[i] >= '1' && raw[i] <= '9') {
        while (i < raw.size() && std::isdigit(static_cast<unsigned char>(raw[i])))
            digits.push_back(raw[i++]);
    } else {
        return false;
    }

    if (i < raw.size() && raw[i] == '.') {
        ++i;
        size_t fracStart = i;
        while (i < raw.size() && std::isdigit(static_cast<unsigned char>(raw[i])))
            digits.push_back(raw[i++]);
        fracLen = static_cast<int>(i - fracStart);
        if (fracLen == 0) return false;
    }

    if (i < raw.size() && (raw[i] == 'e' || raw[i] == 'E')) {
        ++i;
        bool eneg = false;
        if (i < raw.size() && (raw[i] == '+' || raw[i] == '-')) {
            eneg = raw[i] == '-';
            ++i;
        }
        size_t expStart = i;
        long e = 0;
        while (i < raw.size() && std::isdigit(static_cast<unsigned char>(raw[i]))) {
            e = e * 10 + (raw[i] - '0');
            if (e > 100000) return false;
            ++i;
        }
        if (i == expStart) return false;
        exp = eneg ? -static_cast<int>(e) : static_cast<int>(e);
    }
    return i == raw.size();
}

} // namespace

int requiredScale(const std::string& raw) {
    bool neg;
    std::string digits;
    int fracLen, exp;
    if (!parseParts(raw, neg, digits, fracLen, exp)) return -1;
    int places = fracLen - exp;
    return places > 0 ? places : 0;
}

bool decode(const std::string& raw, int scale, int64_t& out) {
    bool negative;
    std::string digits;
    int fracLen, exp;
    if (!parseParts(raw, negative, digits, fracLen, exp)) return false;

    long power = static_cast<long>(exp) - fracLen + scale;
    if (power < 0) return false; // scale smaller than required: not exact

    i128 v = 0;
    for (char c : digits) {
        v = v * 10 + (c - '0');
        if (v > static_cast<i128>(MAX_COORD)) return false;
    }
    for (long k = 0; k < power; ++k) {
        v *= 10;
        if (v > static_cast<i128>(MAX_COORD)) return false;
    }
    out = static_cast<int64_t>(negative ? -v : v);
    return true;
}

} // namespace decimal
