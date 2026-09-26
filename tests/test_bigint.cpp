// test_bigint.cpp — unit tests for BigUint (no framework; asserts + summary).
#include <cassert>
#include <iostream>
#include <stdexcept>
#include <string>

#include "../src/bigint.hpp"

namespace {

int g_failures = 0;

void check(bool cond, const std::string& name) {
    if (!cond) {
        ++g_failures;
        std::cerr << "FAIL: " << name << '\n';
    }
}

BigUint fromStr(const std::string& s) {
    BigUint v;
    bool ok = BigUint::fromString(s, v);
    check(ok, "fromString(\"" + s + "\") should succeed");
    return v;
}

void testZeroAndSmall() {
    check(BigUint().toString() == "0", "default is zero");
    check(BigUint().isZero(), "default isZero");
    check(BigUint(0).toString() == "0", "ctor 0");
    check(BigUint(1).toString() == "1", "ctor 1");
    check(BigUint(999999999).toString() == "999999999", "one limb max");
    check(BigUint(1000000000).toString() == "1000000000", "limb boundary");
    check(BigUint(18446744073709551615ull).toString() == "18446744073709551615",
          "uint64 max roundtrip");
}

void testFromString() {
    check(fromStr("0").toString() == "0", "parse 0");
    check(fromStr("000123").toString() == "123", "leading zeros stripped");
    check(fromStr("1180591620717411303424").toString() == "1180591620717411303424",
          "2^70 roundtrip");
    BigUint v;
    check(!BigUint::fromString("", v), "empty rejected");
    check(!BigUint::fromString("12a3", v), "non-digit rejected");
    check(!BigUint::fromString("-5", v), "sign rejected");
    check(!BigUint::fromString(" 12", v), "space rejected");
}

void testAdd() {
    BigUint a(999999999);
    a += BigUint(1);
    check(a.toString() == "1000000000", "carry across limb boundary");
    BigUint b;  // 0
    b += BigUint(0);
    check(b.isZero(), "0+0==0");
    b += BigUint(42);
    check(b.toString() == "42", "0+42==42");
    BigUint c = fromStr("999999999999999999999999999");
    c += BigUint(1);
    check(c.toString() == "1000000000000000000000000000", "carry cascades all limbs");
    BigUint d = fromStr("123456789012345678901234567890");
    d += fromStr("987654321098765432109876543210");
    check(d.toString() == "1111111110111111111011111111100", "large add");
}

void testSub() {
    BigUint a = fromStr("1000000000");
    a -= BigUint(1);
    check(a.toString() == "999999999", "borrow across limb boundary");
    BigUint b = fromStr("1000000000000000000");
    b -= fromStr("999999999999999999");
    check(b.toString() == "1", "borrow cascades");
    BigUint c = fromStr("123456789123456789");
    c -= fromStr("123456789123456789");
    check(c.isZero(), "x-x==0");
    bool threw = false;
    try {
        BigUint d(1);
        d -= BigUint(2);
    } catch (const std::underflow_error&) {
        threw = true;
    }
    check(threw, "underflow throws");
}

void testCompare() {
    check(BigUint(0) == BigUint(0), "0==0");
    check(BigUint(5) > BigUint(4), "5>4");
    check(BigUint(1000000000) > BigUint(999999999), "limb size dominates");
    check(fromStr("1180591620717411303424") > BigUint(18446744073709551615ull),
          "2^70 > 2^64-1");
    check(fromStr("123") <= fromStr("123"), "<= equal");
    check(!(fromStr("124") <= fromStr("123")), "<= false case");
}

void testAccumulatedSum() {
    // Sum 2^70 by repeated doubling via addition: x += x, 70 times.
    BigUint x(1);
    for (int i = 0; i < 70; ++i) x += x;
    check(x.toString() == "1180591620717411303424", "70 doublings == 2^70");
}

}  // namespace

int main() {
    testZeroAndSmall();
    testFromString();
    testAdd();
    testSub();
    testCompare();
    testAccumulatedSum();
    if (g_failures == 0) {
        std::cout << "test_bigint: all tests passed\n";
        return 0;
    }
    std::cerr << "test_bigint: " << g_failures << " failure(s)\n";
    return 1;
}
