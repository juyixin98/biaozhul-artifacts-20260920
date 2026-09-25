// SPDX-License-Identifier: MIT
// 任意精度有符号整数（base 2^32，小端存储绝对值）。
// 目的：线段相交中间量（坐标差乘积之和）在 int64 输入下会溢出 64 位，
// 全部计算改走 BigInt，保证精确、无溢出、无浮点。
#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace segi {

using U32 = uint32_t;
using U64 = uint64_t;
using Limbs = std::vector<U32>;

class BigInt {
public:
    bool neg = false; // 符号；d 为空表示 0 时 neg 必须为 false
    Limbs d;          // 绝对值，小端，无前导 0

    BigInt() = default;
    BigInt(int64_t v);
    BigInt(bool negative, Limbs mag) : neg(negative), d(std::move(mag)) { normalizeZero(); }

    static BigInt parse(const std::string& decimal); // 十进制字符串
    std::string to_string() const;

    bool isZero() const { return d.empty(); }
    BigInt abs() const { return BigInt(false, d); }
    BigInt operator-() const;

    BigInt& operator+=(const BigInt& o);
    BigInt& operator-=(const BigInt& o);
    BigInt& operator*=(const BigInt& o);
    BigInt& operator/=(const BigInt& o);
    BigInt& operator%=(const BigInt& o);

    friend BigInt operator+(BigInt a, const BigInt& b) { return a += b; }
    friend BigInt operator-(BigInt a, const BigInt& b) { return a -= b; }
    friend BigInt operator*(BigInt a, const BigInt& b) { return a *= b; }
    friend BigInt operator/(BigInt a, const BigInt& b) { return a /= b; }
    friend BigInt operator%(BigInt a, const BigInt& b) { return a %= b; }

    bool operator==(const BigInt& o) const;
    bool operator!=(const BigInt& o) const { return !(*this == o); }
    bool operator<(const BigInt& o) const;
    bool operator<=(const BigInt& o) const { return !(o < *this); }
    bool operator>(const BigInt& o) const { return o < *this; }
    bool operator>=(const BigInt& o) const { return !(*this < o); }

    // 最大公约数（入参取绝对值）
    static BigInt gcd(const BigInt& a, const BigInt& b);

    // ---- 量级（绝对值）原语，供有理数/除法复用 ----
    static int magCmp(const Limbs& a, const Limbs& b);
    static void magAdd(const Limbs& a, const Limbs& b, Limbs& out);
    static void magSub(const Limbs& a, const Limbs& b, Limbs& out); // 要求 a >= b
    static void magMul(const Limbs& a, const Limbs& b, Limbs& out);
    // 二进制长除：返回 (商, 余数)，均为绝对值；b 不得为 0
    static void magDivMod(const Limbs& a, const Limbs& b, Limbs& q, Limbs& r);
    // 小除数（d < 2^32）长除，返回余数；商就地写回
    static U32 magDivSmall(Limbs& a, U32 divisor);

private:
    void normalizeZero() {
        if (d.empty()) neg = false;
    }
    void assignI64(int64_t v);
};

} // namespace segi
