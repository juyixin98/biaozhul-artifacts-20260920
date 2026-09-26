#include "bigint.hpp"

#include <algorithm>
#include <stdexcept>

BigUint::BigUint(uint64_t value) {
    while (value > 0) {
        limbs_.push_back(static_cast<uint32_t>(value % kBase));
        value /= kBase;
    }
}

bool BigUint::fromString(const std::string& s, BigUint& out) {
    if (s.empty()) return false;
    for (char c : s) {
        if (c < '0' || c > '9') return false;
    }
    BigUint result;
    // Process 9 digits at a time: result = result * 10^chunk_len + chunk.
    size_t pos = 0;
    while (pos < s.size()) {
        size_t len = std::min<size_t>(9, s.size() - pos);
        uint32_t chunk = 0;
        for (size_t i = 0; i < len; ++i) {
            chunk = chunk * 10u + static_cast<uint32_t>(s[pos + i] - '0');
        }
        uint32_t multiplier = 1;
        for (size_t i = 0; i < len; ++i) multiplier *= 10u;
        // result *= multiplier (single-limb multiply, inlined to avoid a
        // full multiplication routine we do not otherwise need).
        uint64_t carry = 0;
        for (auto& limb : result.limbs_) {
            uint64_t cur = static_cast<uint64_t>(limb) * multiplier + carry;
            limb = static_cast<uint32_t>(cur % kBase);
            carry = cur / kBase;
        }
        while (carry > 0) {
            result.limbs_.push_back(static_cast<uint32_t>(carry % kBase));
            carry /= kBase;
        }
        result += BigUint(chunk);
        pos += len;
    }
    out = result;
    return true;
}

std::string BigUint::toString() const {
    if (limbs_.empty()) return "0";
    std::string s = std::to_string(limbs_.back());
    for (size_t i = limbs_.size() - 1; i-- > 0;) {
        std::string part = std::to_string(limbs_[i]);
        s.append(9 - part.size(), '0');
        s += part;
    }
    return s;
}

int BigUint::compare(const BigUint& other) const {
    if (limbs_.size() != other.limbs_.size()) {
        return limbs_.size() < other.limbs_.size() ? -1 : 1;
    }
    for (size_t i = limbs_.size(); i-- > 0;) {
        if (limbs_[i] != other.limbs_[i]) {
            return limbs_[i] < other.limbs_[i] ? -1 : 1;
        }
    }
    return 0;
}

BigUint& BigUint::operator+=(const BigUint& other) {
    if (other.limbs_.size() > limbs_.size()) {
        limbs_.resize(other.limbs_.size(), 0);
    }
    uint64_t carry = 0;
    for (size_t i = 0; i < limbs_.size(); ++i) {
        uint64_t cur = static_cast<uint64_t>(limbs_[i]) + carry;
        if (i < other.limbs_.size()) cur += other.limbs_[i];
        limbs_[i] = static_cast<uint32_t>(cur % kBase);
        carry = cur / kBase;
    }
    if (carry > 0) limbs_.push_back(static_cast<uint32_t>(carry));
    return *this;
}

BigUint& BigUint::operator-=(const BigUint& other) {
    if (*this < other) {
        throw std::underflow_error("BigUint subtraction underflow");
    }
    int64_t borrow = 0;
    for (size_t i = 0; i < limbs_.size(); ++i) {
        int64_t cur = static_cast<int64_t>(limbs_[i]) - borrow;
        if (i < other.limbs_.size()) cur -= other.limbs_[i];
        if (cur < 0) {
            cur += kBase;
            borrow = 1;
        } else {
            borrow = 0;
        }
        limbs_[i] = static_cast<uint32_t>(cur);
    }
    normalize();
    return *this;
}

void BigUint::normalize() {
    while (!limbs_.empty() && limbs_.back() == 0) limbs_.pop_back();
}
