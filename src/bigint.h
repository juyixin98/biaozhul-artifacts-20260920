// SPDX-License-Identifier: MIT
// Minimal arbitrary-precision unsigned integer for DAG path counts.
// Base-1e9 limbs, little-endian. Only the operations needed by this project.
#ifndef DAGPATHS_BIGINT_H
#define DAGPATHS_BIGINT_H

#include <cstdint>
#include <string>
#include <vector>
#include <algorithm>
#include <stdexcept>

namespace dagpaths {

class BigInt {
public:
    BigInt() : limbs_(1, 0) {}
    BigInt(uint64_t value) {  // implicit on purpose: literals mix freely
        if (value == 0) {
            limbs_.push_back(0);
        } else {
            while (value > 0) {
                limbs_.push_back(static_cast<uint32_t>(value % BASE));
                value /= BASE;
            }
        }
    }

    static BigInt zero() { return BigInt(); }
    static BigInt one() { return BigInt(1); }

    BigInt& operator+=(const BigInt& other) {
        ensureSize(other.limbs_.size());
        uint64_t carry = 0;
        for (size_t i = 0; i < limbs_.size(); ++i) {
            uint64_t sum = static_cast<uint64_t>(limbs_[i]) + carry;
            if (i < other.limbs_.size()) sum += other.limbs_[i];
            limbs_[i] = static_cast<uint32_t>(sum % BASE);
            carry = sum / BASE;
        }
        if (carry > 0) limbs_.push_back(static_cast<uint32_t>(carry));
        return *this;
    }

    BigInt operator+(const BigInt& other) const {
        BigInt result = *this;
        result += other;
        return result;
    }

    // Exact subtraction. Precondition: *this >= other (the k-th path
    // walk always subtracts a smaller subtree size from a positive rank).
    BigInt& operator-=(const BigInt& other) {
        int64_t borrow = 0;
        for (size_t i = 0; i < limbs_.size(); ++i) {
            int64_t cur = static_cast<int64_t>(limbs_[i]) - borrow;
            if (i < other.limbs_.size()) cur -= other.limbs_[i];
            if (cur < 0) { cur += BASE; borrow = 1; } else borrow = 0;
            limbs_[i] = static_cast<uint32_t>(cur);
        }
        if (borrow != 0) throw std::invalid_argument("BigInt underflow");
        normalize();
        return *this;
    }

    BigInt operator-(const BigInt& other) const {
        BigInt result = *this;
        result -= other;
        return result;
    }

    BigInt& operator++() { *this += BigInt(1); return *this; }

    bool operator==(const BigInt& other) const { return limbs_ == other.limbs_; }
    bool operator!=(const BigInt& other) const { return !(*this == other); }

    bool operator<(const BigInt& other) const {
        if (limbs_.size() != other.limbs_.size())
            return limbs_.size() < other.limbs_.size();
        for (size_t i = limbs_.size(); i-- > 0;) {
            if (limbs_[i] != other.limbs_[i]) return limbs_[i] < other.limbs_[i];
        }
        return false;
    }
    bool operator<=(const BigInt& other) const { return !(other < *this); }
    bool operator>=(const BigInt& other) const { return !(*this < other); }
    bool operator>(const BigInt& other) const { return other < *this; }

    bool isZero() const { return limbs_.size() == 1 && limbs_[0] == 0; }

    std::string str() const {
        std::string out = std::to_string(limbs_.back());
        for (size_t i = limbs_.size() - 1; i-- > 0;) {
            std::string part = std::to_string(limbs_[i]);
            out.append(9 - part.size(), '0');
            out += part;
        }
        return out;
    }

    // Parse a non-negative decimal string; throws std::invalid_argument.
    static BigInt parseDecimal(const std::string& text) {
        if (text.empty()) throw std::invalid_argument("empty number");
        BigInt result;
        result.limbs_.clear();
        size_t end = text.size();
        while (end > 0) {
            const size_t start = (end >= CHUNK) ? end - CHUNK : 0;
            uint32_t limb = 0;
            for (size_t j = start; j < end; ++j) {
                const char c = text[j];
                if (c < '0' || c > '9')
                    throw std::invalid_argument("non-digit in number: " + text);
                limb = limb * 10 + static_cast<uint32_t>(c - '0');
            }
            result.limbs_.push_back(limb);
            end = start;
        }
        result.normalize();
        return result;
    }

    // Number of decimal digits (0 -> 1).
    size_t digits() const {
        size_t n = (limbs_.size() - 1) * 9 + std::to_string(limbs_.back()).size();
        return n;
    }

private:
    static constexpr uint32_t BASE = 1000000000;
    static constexpr size_t CHUNK = 9;
    std::vector<uint32_t> limbs_;

    void ensureSize(size_t size) {
        if (limbs_.size() < size) limbs_.resize(size, 0);
    }
    void normalize() {
        while (limbs_.size() > 1 && limbs_.back() == 0) limbs_.pop_back();
    }
};

} // namespace dagpaths

#endif
