// Minimal test harness shared by the test executables.
#pragma once

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <string>

namespace pf_test {

inline int& failures() {
    static int n = 0;
    return n;
}

inline void report(bool ok, const char* expr, const char* file, int line,
                   const std::string& detail = "") {
    if (!ok) {
        ++failures();
        std::fprintf(stderr, "FAIL %s:%d: %s%s%s\n", file, line, expr,
                     detail.empty() ? "" : "  ", detail.c_str());
    }
}

}  // namespace pf_test

#define CHECK(cond) \
    pf_test::report(static_cast<bool>(cond), #cond, __FILE__, __LINE__)

#define CHECK_CLOSE(a, b, tol)                                            \
    do {                                                                  \
        double _va = static_cast<double>(a);                              \
        double _vb = static_cast<double>(b);                              \
        bool _ok = std::fabs(_va - _vb) <= (tol);                         \
        pf_test::report(_ok, #a " ~= " #b, __FILE__, __LINE__,            \
                        "got " + std::to_string(_va) + " vs " +           \
                            std::to_string(_vb));                         \
    } while (0)

#define RUN_TEST(fn)                       \
    do {                                   \
        std::fprintf(stderr, "- %s\n", #fn); \
        fn();                              \
    } while (0)

#define TEST_MAIN_RETURN() (pf_test::failures() == 0 ? 0 : 1)
