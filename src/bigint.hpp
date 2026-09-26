// bigint.hpp — minimal arbitrary-precision unsigned integer.
//
// Used for path counts and for the 1-based rank K, both of which can
// exceed 64 bits even on moderately sized DAGs. Only the operations the
// DAG algorithms need are implemented: add, sub (when lhs >= rhs),
// compare, zero test, and decimal string conversion.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

class BigUint {
public:
    BigUint() = default;
    explicit BigUint(uint64_t value);

    // Parses a non-empty decimal string (digits only, no sign, no leading
    // whitespace). Returns false on any invalid character or empty input.
    static bool fromString(const std::string& s, BigUint& out);

    std::string toString() const;
    bool isZero() const { return limbs_.empty(); }

    // Returns negative / 0 / positive like memcmp.
    int compare(const BigUint& other) const;

    BigUint& operator+=(const BigUint& other);
    // Precondition: *this >= other.
    BigUint& operator-=(const BigUint& other);

    friend bool operator==(const BigUint& a, const BigUint& b) { return a.compare(b) == 0; }
    friend bool operator<(const BigUint& a, const BigUint& b) { return a.compare(b) < 0; }
    friend bool operator>(const BigUint& a, const BigUint& b) { return a.compare(b) > 0; }
    friend bool operator<=(const BigUint& a, const BigUint& b) { return a.compare(b) <= 0; }

private:
    // Little-endian limbs in base 1e9. Invariant: no trailing zero limbs;
    // zero is represented by an empty vector.
    static constexpr uint32_t kBase = 1000000000u;
    std::vector<uint32_t> limbs_;

    void normalize();
};
