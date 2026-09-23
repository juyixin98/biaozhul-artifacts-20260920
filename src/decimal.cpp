#include "decimal.hpp"

#include <cctype>
#include <stdexcept>

Int pow10Int(long n) {
    if (n < 0) throw std::runtime_error("pow10Int: negative exponent");
    Int r = 1;
    // Exponentiation by squaring with factor 10.
    Int base = 10;
    while (n > 0) {
        if (n & 1) r *= base;
        base *= base;
        n >>= 1;
    }
    return r;
}

Decimal parseDecimal(const std::string& token) {
    Decimal d;
    d.token = token;
    size_t i = 0;
    const size_t n = token.size();
    if (n == 0) throw std::runtime_error("empty number token");

    if (token[i] == '-' || token[i] == '+') {
        d.neg = (token[i] == '-');
        ++i;
    }
    if (i == n) throw std::runtime_error("malformed number: " + token);

    // Reject Infinity / NaN up front.
    std::string rest = token.substr(i);
    for (char& c : rest) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    if (rest.rfind("inf", 0) == 0 || rest.rfind("nan", 0) == 0)
        throw std::runtime_error("non-finite number: " + token);

    std::string mant;
    bool sawDigit = false;
    long fracDigits = 0;
    bool inFrac = false;
    while (i < n) {
        char c = token[i];
        if (std::isdigit(static_cast<unsigned char>(c))) {
            sawDigit = true;
            mant.push_back(c);
            if (inFrac) ++fracDigits;
            ++i;
        } else if (c == '.') {
            if (inFrac) throw std::runtime_error("multiple decimal points: " + token);
            inFrac = true;
            ++i;
        } else if (c == 'e' || c == 'E') {
            ++i;
            break;
        } else {
            throw std::runtime_error("malformed number: " + token);
        }
    }
    if (!sawDigit) throw std::runtime_error("number without digits: " + token);

    long exp = 0;
    if (i < n) {
        int esign = 1;
        if (token[i] == '-' || token[i] == '+') {
            esign = (token[i] == '-') ? -1 : 1;
            ++i;
        }
        if (i == n) throw std::runtime_error("bad exponent: " + token);
        long ev = 0;
        while (i < n) {
            char c = token[i];
            if (!std::isdigit(static_cast<unsigned char>(c)))
                throw std::runtime_error("bad exponent: " + token);
            // Cap parsed magnitude to avoid overflow; coordinates this extreme
            // are not meaningful for this service.
            if (ev > 1000000) throw std::runtime_error("exponent too large: " + token);
            ev = ev * 10 + (c - '0');
            ++i;
        }
        exp = esign * ev;
    }

    // value = mantissa * 10^(exp - fracDigits), with leading zeros stripped.
    size_t first = mant.find_first_not_of('0');
    if (first == std::string::npos) {
        d.digits = 0;
        d.neg = false; // normalize -0
        d.exp10 = 0;
        return d;
    }
    size_t last = mant.find_last_not_of('0');
    std::string sig = mant.substr(first, last - first + 1);
    long trailing = static_cast<long>(mant.size() - 1 - last);
    d.digits = Int(sig);
    d.exp10 = exp - fracDigits + trailing;
    return d;
}

Int Decimal::scaled(long S) const {
    if (digits == 0) return Int(0);
    long p = exp10 + S;
    Int v;
    if (p >= 0) {
        v = digits * pow10Int(p);
    } else {
        // p < 0 means S was not large enough for this number.
        throw std::runtime_error("scaled() called with insufficient common scale");
    }
    return neg ? -v : v;
}

long commonScale(const std::vector<Decimal>& nums) {
    long S = 0;
    for (const auto& d : nums) {
        if (d.digits == 0) continue;
        if (d.exp10 < 0 && -d.exp10 > S) S = -d.exp10;
    }
    return S;
}

std::string formatScaled(const Int& scaledValue, long S) {
    if (S <= 0) return scaledValue.str();
    bool neg = scaledValue < 0;
    Int a = neg ? -scaledValue : scaledValue;
    std::string digits = a.str();
    std::string out;
    if (static_cast<long>(digits.size()) <= S) {
        out = "0." + std::string(static_cast<size_t>(S) - digits.size(), '0') + digits;
    } else {
        out = digits.substr(0, digits.size() - static_cast<size_t>(S)) + "." +
              digits.substr(digits.size() - static_cast<size_t>(S));
    }
    if (neg) out = "-" + out;
    return out;
}
