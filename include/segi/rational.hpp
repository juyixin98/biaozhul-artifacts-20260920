// SPDX-License-Identifier: MIT
// 有理数（BigInt / BigInt），始终约分，分母恒正。
#pragma once

#include "segi/bigint.hpp"

#include <stdexcept>
#include <string>

namespace segi {

struct Rat {
    BigInt n; // 分子
    BigInt d; // 分母，恒为正

    Rat() : n(0), d(1) {}
    Rat(int64_t v) : n(v), d(1) {}
    Rat(BigInt num, BigInt den) : n(std::move(num)), d(std::move(den)) { normalize(); }

    bool isZero() const { return n.isZero(); }
    void normalize() {
        if (d.isZero()) throw std::domain_error("Rat: zero denominator");
        if (d.neg) { n = -n; d.neg = false; }
        if (n.isZero()) { d = BigInt(1); return; }
        BigInt g = BigInt::gcd(n.abs(), d.abs());
        if (g != BigInt(1)) { n /= g; d /= g; }
    }

    int sign() const { return n.neg ? -1 : (n.isZero() ? 0 : 1); }

    static int cmp(const Rat& a, const Rat& b) {
        BigInt lhs = a.n * b.d, rhs = b.n * a.d; // 交叉乘，两分母均为正
        if (lhs < rhs) return -1;
        if (rhs < lhs) return 1;
        return 0;
    }

    friend Rat operator+(const Rat& a, const Rat& b) {
        return Rat(a.n * b.d + b.n * a.d, a.d * b.d);
    }
    friend Rat operator*(const Rat& a, const Rat& b) {
        return Rat(a.n * b.n, a.d * b.d);
    }
    friend bool operator==(const Rat& a, const Rat& b) { return cmp(a, b) == 0; }
    friend bool operator!=(const Rat& a, const Rat& b) { return cmp(a, b) != 0; }
    friend bool operator<(const Rat& a, const Rat& b) { return cmp(a, b) < 0; }
    friend bool operator>(const Rat& a, const Rat& b) { return cmp(a, b) > 0; }
    friend bool operator<=(const Rat& a, const Rat& b) { return cmp(a, b) <= 0; }
    friend bool operator>=(const Rat& a, const Rat& b) { return cmp(a, b) >= 0; }

    std::string str() const { return n.to_string() + "/" + d.to_string(); }
};

} // namespace segi
