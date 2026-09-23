// Exact decimal parsing.
//
// Input numbers are finite decimal literals (optionally with an exponent).
// We never parse coordinates through floating point. Each number is stored
// as sign * digits * 10^exp10, and a whole request is scaled by one common
// factor 10^S so that every coordinate becomes an integer. All geometric
// predicates then run on arbitrary-precision integers (cpp_int).
#pragma once

#include <boost/multiprecision/cpp_int.hpp>

#include <string>
#include <vector>

using Int = boost::multiprecision::cpp_int;

struct Decimal {
    bool neg = false;
    Int digits;          // significant digits, no leading zeros (0 for zero)
    long exp10 = 0;      // value = sign * digits * 10^exp10
    std::string token;   // original source token

    Int scaled(long S) const; // value * 10^S (always integral when S chosen via commonScale)
};

// Parse one finite decimal token. Throws std::runtime_error on malformed input
// (Infinity/NaN are rejected).
Decimal parseDecimal(const std::string& token);

// Given a list of decimals, pick the smallest S >= 0 such that every
// value * 10^S is an integer.
long commonScale(const std::vector<Decimal>& nums);

Int pow10Int(long n);

// Render a non-negative scaled integer with an implicit decimal point:
// S digits after the point (pads / trims nothing; S >= 0).
std::string formatScaled(const Int& scaledValue, long S);
