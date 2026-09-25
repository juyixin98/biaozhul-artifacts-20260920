//
// framework.hpp — tiny dependency-free unit-test framework offering the
// subset of GoogleTest macros used by this project:
//   TEST(Suite, Name) { ... }
//   EXPECT_EQ / ASSERT_EQ / ASSERT_TRUE / EXPECT_TRUE
//
// ASSERT_* failures abort the current test by throwing; EXPECT_* failures
// are recorded but the test continues.
//
#pragma once

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <exception>
#include <functional>
#include <sstream>
#include <string>
#include <vector>

namespace ru::test {

using i128 = __int128_t;

struct AssertionFailure : std::exception {
    std::string message;
    explicit AssertionFailure(std::string m) : message(std::move(m)) {}
    const char* what() const noexcept override { return message.c_str(); }
};

struct TestCase {
    std::string suite;
    std::string name;
    std::function<void()> fn;
};

inline std::vector<TestCase>& registry() {
    static std::vector<TestCase> r;
    return r;
}

struct Registrar {
    Registrar(const char* suite, const char* name, std::function<void()> fn) {
        registry().push_back({suite, name, std::move(fn)});
    }
};

template <typename T>
std::string toString(const T& v) {
    std::ostringstream ss;
    ss << v;
    return ss.str();
}

inline std::string toString(i128 v) {
    if (v == 0) return "0";
    bool neg = v < 0;
    std::string s;
    i128 a = neg ? -v : v;
    while (a > 0) {
        s.push_back(static_cast<char>('0' + static_cast<int>(a % 10)));
        a /= 10;
    }
    if (neg) s.push_back('-');
    std::reverse(s.begin(), s.end());
    return s;
}

template <typename A, typename B>
void expectEq(const A& a, const B& b, const char* sa, const char* sb,
              const char* file, int line, bool fatal) {
    if (a == b) return;
    std::ostringstream ss;
    ss << file << ":" << line << ": Failure\n"
       << "  " << sa << " == " << sb << "\n"
       << "  " << sa << " = " << toString(a) << "\n"
       << "  " << sb << " = " << toString(b);
    (void)fatal;
    throw AssertionFailure(ss.str());  // caller decides: ASSERT propagates,
                                       // EXPECT catches and keeps going
}

inline int runAll(int argc, char** argv) {
    (void)argc; (void)argv;
    int passed = 0, failed = 0;
    std::string lastSuite;
    for (const TestCase& tc : registry()) {
        if (tc.suite != lastSuite) {
            std::printf("[----------] %s\n", tc.suite.c_str());
            lastSuite = tc.suite;
        }
        bool ok = true;
        try {
            tc.fn();
        } catch (const AssertionFailure& f) {
            ok = false;
            std::printf("%s\n", f.what());
        } catch (const std::exception& e) {
            ok = false;
            std::printf("unexpected exception: %s\n", e.what());
        }
        if (ok) {
            std::printf("[       OK ] %s.%s\n", tc.suite.c_str(), tc.name.c_str());
            ++passed;
        } else {
            std::printf("[  FAILED  ] %s.%s\n", tc.suite.c_str(), tc.name.c_str());
            ++failed;
        }
    }
    std::printf("\n[==========] %d test(s); %d passed, %d failed\n",
                passed + failed, passed, failed);
    return failed == 0 ? 0 : 1;
}

}  // namespace ru::test

#define RU_CONCAT_INNER(a, b) a##b
#define RU_CONCAT(a, b) RU_CONCAT_INNER(a, b)

#define TEST(suite, name)                                                          \
    static void RU_CONCAT(rutest_, __LINE__)();                                    \
    static ::ru::test::Registrar RU_CONCAT(reg_, __LINE__)(                        \
        #suite, #name, &RU_CONCAT(rutest_, __LINE__));                             \
    static void RU_CONCAT(rutest_, __LINE__)()

#define EXPECT_EQ(a, b)                                                            \
    do {                                                                           \
        try {                                                                      \
            ::ru::test::expectEq((a), (b), #a, #b, __FILE__, __LINE__, false);     \
        } catch (const ::ru::test::AssertionFailure& f_) {                         \
            std::printf("%s\n", f_.what());                                        \
        }                                                                          \
    } while (0)

#define ASSERT_EQ(a, b) ::ru::test::expectEq((a), (b), #a, #b, __FILE__, __LINE__, true)
#define EXPECT_TRUE(x) EXPECT_EQ((x), true)
#define ASSERT_TRUE(x) ASSERT_EQ((x), true)
