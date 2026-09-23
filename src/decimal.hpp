// Exact decimal parsing.
//
// JSON numbers are accepted as decimal literals (optional fraction and
// exponent) and converted to int64_t coordinates by a request-wide common
// scale of 10^K. Mixed precisions in one request are aligned automatically:
// e.g. 1.2 and 1.234 with K=3 become 1200 and 1234.
//
// Limits (see README "精度与取值范围"):
//   * at most 12 digits after the decimal point after applying the exponent
//     (i.e. effective K <= 12);
//   * scaled coordinate magnitude <= 4.6e18, so every orientation product
//     fits in signed 128 bits with margin.
#pragma once

#include <cstdint>
#include <string>

namespace decimal {

constexpr int MAX_SCALE = 12;
constexpr int64_t MAX_COORD = 4600000000000000000LL; // 4.6e18

// Returns the number of negative decimal places implied by this number text
// (0 for integers), or -1 if the text is not a valid JSON number literal.
int requiredScale(const std::string& raw);

// Decode raw into a coordinate divided by 10^scale, i.e.
// result = value * 10^scale. Returns false if the result would not fit in
// int64 / MAX_COORD. raw must have been accepted by requiredScale.
bool decode(const std::string& raw, int scale, int64_t& out);

} // namespace decimal
