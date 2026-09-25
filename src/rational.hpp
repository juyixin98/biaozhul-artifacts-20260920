// rational.hpp — 精确有理数（任意精度），用于交点坐标的无损输出。
//
// 设计说明：
//  - 底层整数为 boost::multiprecision::cpp_int（任意精度，header-only）。
//  - 所有有理数在构造时即约分为最简分数，且分母恒为正。
//  - 全程不使用浮点数，杜绝精度损失。
#pragma once

#include <boost/multiprecision/cpp_int.hpp>
#include <string>

namespace segint {

using boost::multiprecision::cpp_int;

// 最大公约数（欧几里得算法），要求 a >= 0, b >= 0。
inline cpp_int gcd_int(cpp_int a, cpp_int b) {
    if (a < 0) a = -a;
    if (b < 0) b = -b;
    while (b != 0) {
        cpp_int r = a % b;
        a = b;
        b = r;
    }
    return a;
}

// 最简有理数：den > 0，gcd(|num|, den) == 1；0 表示为 0/1。
struct Rational {
    cpp_int num = 0;
    cpp_int den = 1;

    Rational() = default;
    Rational(long long v) : num(v), den(1) {}
    Rational(cpp_int n, cpp_int d) {
        if (d == 0) {
            // 调用方保证不会发生；防御性处理。
            num = 0;
            den = 1;
            return;
        }
        if (d < 0) { n = -n; d = -d; }
        if (n == 0) { num = 0; den = 1; return; }
        cpp_int g = gcd_int(n < 0 ? -n : n, d);
        num = n / g;
        den = d / g;
    }

    bool operator==(const Rational& o) const { return num == o.num && den == o.den; }
    bool operator!=(const Rational& o) const { return !(*this == o); }

    std::string num_str() const { return num.convert_to<std::string>(); }
    std::string den_str() const { return den.convert_to<std::string>(); }
};

}  // namespace segint
