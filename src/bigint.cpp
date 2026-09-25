// SPDX-License-Identifier: MIT
#include "segi/bigint.hpp"

#include <algorithm>
#include <stdexcept>

namespace segi {

namespace {
inline bool bit(const Limbs& a, size_t i) {
    return (a[i >> 5] >> (i & 31)) & 1u;
}
inline void setBit(Limbs& a, size_t i) {
    if (i >> 5 >= a.size()) a.resize((i >> 5) + 1, 0);
    a[i >> 5] |= U32(1) << (i & 31);
}
// 左移 1 位，进位 c（0/1）入最低位 —— 用于二进制除法余数寄存器
inline void shl1In(Limbs& a, unsigned c) {
    U64 carry = c;
    for (size_t i = 0; i < a.size(); ++i) {
        U64 v = (U64(a[i]) << 1) | carry;
        a[i] = U32(v);
        carry = v >> 32;
    }
    if (carry) a.push_back(1);
}
} // namespace

BigInt::BigInt(int64_t v) { assignI64(v); }

void BigInt::assignI64(int64_t v) {
    neg = false;
    d.clear();
    if (v == 0) return;
    uint64_t m;
    if (v < 0) {
        neg = true;
        m = uint64_t(-(v + 1)) + 1u; // 避开 -INT64_MIN 的 UB
    } else {
        m = uint64_t(v);
    }
    d.push_back(U32(m));
    if (m >> 32) d.push_back(U32(m >> 32));
}

BigInt BigInt::operator-() const {
    BigInt r = *this;
    if (!r.isZero()) r.neg = !r.neg;
    return r;
}

// ---------------- 量级运算 ----------------

int BigInt::magCmp(const Limbs& a, const Limbs& b) {
    if (a.size() != b.size()) return a.size() < b.size() ? -1 : 1;
    for (size_t i = a.size(); i-- > 0;)
        if (a[i] != b[i]) return a[i] < b[i] ? -1 : 1;
    return 0;
}

void BigInt::magAdd(const Limbs& a, const Limbs& b, Limbs& out) {
    size_t n = std::max(a.size(), b.size());
    out.assign(n + 1, 0);
    U64 carry = 0;
    for (size_t i = 0; i < n; ++i) {
        U64 v = carry + (i < a.size() ? a[i] : 0) + (i < b.size() ? b[i] : 0);
        out[i] = U32(v);
        carry = v >> 32;
    }
    out[n] = U32(carry);
    while (!out.empty() && out.back() == 0) out.pop_back();
}

void BigInt::magSub(const Limbs& a, const Limbs& b, Limbs& out) {
    // 调用方保证 a >= b
    out.assign(a.size(), 0);
    int64_t borrow = 0;
    for (size_t i = 0; i < a.size(); ++i) {
        int64_t v = int64_t(a[i]) - borrow - int64_t(i < b.size() ? b[i] : 0);
        if (v < 0) { v += (int64_t(1) << 32); borrow = 1; }
        else borrow = 0;
        out[i] = U32(v);
    }
    while (!out.empty() && out.back() == 0) out.pop_back();
}

void BigInt::magMul(const Limbs& a, const Limbs& b, Limbs& out) {
    out.assign(a.size() + b.size(), 0);
    for (size_t i = 0; i < a.size(); ++i) {
        U64 carry = 0;
        for (size_t j = 0; j < b.size(); ++j) {
            U64 v = U64(a[i]) * b[j] + out[i + j] + carry; // ≤ (2^32-1)^2 + 2*(2^32-1) < 2^64
            out[i + j] = U32(v);
            carry = v >> 32;
        }
        out[i + b.size()] = U32(carry);
    }
    while (!out.empty() && out.back() == 0) out.pop_back();
}

void BigInt::magDivMod(const Limbs& a, const Limbs& b, Limbs& q, Limbs& r) {
    // 教科书式二进制长除法（按位移位），a、b 均为非零量级
    if (magCmp(a, b) < 0) { q.clear(); r = a; return; }
    q.clear();
    r.clear();
    size_t bits = a.size() * 32;
    for (size_t k = bits; k-- > 0;) {
        shl1In(r, bit(a, k));
        if (magCmp(r, b) >= 0) {
            Limbs t;
            magSub(r, b, t);
            r.swap(t);
            setBit(q, k);
        }
    }
    while (!q.empty() && q.back() == 0) q.pop_back();
    while (!r.empty() && r.back() == 0) r.pop_back();
}

U32 BigInt::magDivSmall(Limbs& a, U32 divisor) {
    U64 rem = 0;
    for (size_t i = a.size(); i-- > 0;) {
        U64 v = (rem << 32) | a[i];
        a[i] = U32(v / divisor);
        rem = v % divisor;
    }
    while (!a.empty() && a.back() == 0) a.pop_back();
    return U32(rem);
}

// ---------------- 带符号四则运算 ----------------

BigInt& BigInt::operator+=(const BigInt& o) {
    if (o.isZero()) return *this;
    if (isZero()) { *this = o; return *this; }
    if (neg == o.neg) {
        Limbs m;
        magAdd(d, o.d, m);
        d = std::move(m);
    } else {
        int c = magCmp(d, o.d);
        if (c == 0) { d.clear(); neg = false; return *this; }
        Limbs m;
        if (c > 0) magSub(d, o.d, m);
        else { magSub(o.d, d, m); neg = !neg; }
        d = std::move(m);
    }
    normalizeZero();
    return *this;
}

BigInt& BigInt::operator-=(const BigInt& o) {
    BigInt no = o;
    if (!no.isZero()) no.neg = !no.neg;
    return *this += no;
}

BigInt& BigInt::operator*=(const BigInt& o) {
    Limbs m;
    magMul(d, o.d, m);
    d = std::move(m);
    neg = neg != o.neg;
    normalizeZero();
    return *this;
}

BigInt& BigInt::operator/=(const BigInt& o) {
    if (o.isZero()) throw std::domain_error("BigInt: division by zero");
    if (isZero()) return *this;
    Limbs qq, rr;
    magDivMod(d, o.d, qq, rr);
    d = std::move(qq);
    neg = neg != o.neg; // 向零截断：绝对值商 + 结果符号
    normalizeZero();
    return *this;
}

BigInt& BigInt::operator%=(const BigInt& o) {
    if (o.isZero()) throw std::domain_error("BigInt: modulo by zero");
    if (isZero()) return *this;
    Limbs qq, rr;
    magDivMod(d, o.d, qq, rr);
    d = std::move(rr);
    neg = neg; // 截断式取余：余数符号跟随被除数
    normalizeZero();
    return *this;
}

// ---------------- 比较 ----------------

bool BigInt::operator==(const BigInt& o) const {
    return neg == o.neg && magCmp(d, o.d) == 0;
}

bool BigInt::operator<(const BigInt& o) const {
    if (neg != o.neg) {
        if (isZero()) return !o.neg && !o.isZero();       // 0 < 正
        if (o.isZero()) return neg;                        // 负 < 0
        return neg;                                        // 负 < 正
    }
    int c = magCmp(d, o.d);
    return neg ? c > 0 : c < 0;
}

// ---------------- gcd（二进制 GCD，量级上做） ----------------

BigInt BigInt::gcd(const BigInt& a, const BigInt& b) {
    Limbs x = a.d, y = b.d;
    if (x.empty()) return BigInt(false, y);
    if (y.empty()) return BigInt(false, x);
    size_t shift = 0;
    while (true) {
        bool xEven = (x[0] & 1u) == 0;
        bool yEven = (y[0] & 1u) == 0;
        if (xEven && yEven) {
            // 各除 2
            U64 c;
            c = 0; for (size_t i = x.size(); i-- > 0;) { U64 v = (c << 32) | x[i]; x[i] = U32(v >> 1); c = v & 1; }
            c = 0; for (size_t i = y.size(); i-- > 0;) { U64 v = (c << 32) | y[i]; y[i] = U32(v >> 1); c = v & 1; }
            ++shift;
        } else if (xEven) {
            U64 c = 0; for (size_t i = x.size(); i-- > 0;) { U64 v = (c << 32) | x[i]; x[i] = U32(v >> 1); c = v & 1; }
        } else if (yEven) {
            U64 c = 0; for (size_t i = y.size(); i-- > 0;) { U64 v = (c << 32) | y[i]; y[i] = U32(v >> 1); c = v & 1; }
        } else {
            int cmp = magCmp(x, y);
            if (cmp == 0) break;
            Limbs t; // 输出不得与输入别名
            if (cmp > 0) { magSub(x, y, t); x.swap(t); }
            else { magSub(y, x, t); y.swap(t); }
        }
        while (!x.empty() && x.back() == 0) x.pop_back();
        while (!y.empty() && y.back() == 0) y.pop_back();
        if (x.empty() || y.empty()) { x = x.empty() ? y : x; break; }
    }
    // x *= 2^shift（按 limb 进位，b=0 时 carry 恒为 0，逻辑同样成立）
    if (shift) {
        size_t w = shift >> 5, b = shift & 31;
        Limbs r(x.size() + w + 1, 0);
        U32 carry = 0;
        for (size_t i = 0; i < x.size(); ++i) {
            U64 v = (U64(x[i]) << b) | carry;
            r[i + w] = U32(v);
            carry = U32(v >> 32);
        }
        r[x.size() + w] = carry;
        while (!r.empty() && r.back() == 0) r.pop_back();
        x = std::move(r);
    }
    return BigInt(false, x);
}

// ---------------- 十进制转换 ----------------

std::string BigInt::to_string() const {
    if (isZero()) return "0";
    Limbs m = d;
    std::string s;
    s.reserve(m.size() * 10);
    while (!m.empty()) {
        U32 rem = magDivSmall(m, 10);
        s.push_back(char('0' + rem));
    }
    if (neg) s.push_back('-');
    std::reverse(s.begin(), s.end());
    return s;
}

BigInt BigInt::parse(const std::string& s0) {
    std::string s = s0;
    bool negative = false;
    if (!s.empty() && (s[0] == '-' || s[0] == '+')) {
        negative = s[0] == '-';
        s.erase(s.begin());
    }
    if (s.empty()) throw std::invalid_argument("BigInt::parse: empty number");
    Limbs m; // 逐位累乘 10
    for (char c : s) {
        if (c < '0' || c > '9') throw std::invalid_argument("BigInt::parse: bad digit in '" + s0 + "'");
        // m = m*10 + digit（量级乘法）
        Limbs ten{10}, prod;
        magMul(m, ten, prod);
        Limbs add;
        magAdd(prod, Limbs{U32(c - '0')}, add);
        m = std::move(add);
    }
    BigInt r(false, std::move(m));
    if (r.isZero()) negative = false;
    r.neg = negative;
    return r;
}

} // namespace segi
