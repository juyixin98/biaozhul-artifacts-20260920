// Known-answer tests for the self-contained SHA-256 implementation (FIPS
// 180-4 vectors), plus streaming/one-shot consistency around block and
// padding boundaries (no guessed hashes for the boundary cases).
#include <cstdio>
#include <string>

#include "sha256.hpp"

using pcrs::Sha256;

static int failures = 0;

static void check(const std::string& input, const std::string& expected,
                  const char* label) {
    const std::string got = Sha256::hex(input);
    if (got != expected) {
        std::printf("FAIL %s\n  expected %s\n  got      %s\n", label,
                    expected.c_str(), got.c_str());
        ++failures;
    } else {
        std::printf("ok   %s\n", label);
    }
}

static std::string toHex(const std::array<uint8_t, 32>& d) {
    static const char* hexd = "0123456789abcdef";
    std::string s;
    s.reserve(64);
    for (unsigned char b : d) {
        s += hexd[b >> 4];
        s += hexd[b & 0xF];
    }
    return s;
}

int main() {
    // FIPS 180-4 B.1: "abc"
    check("abc",
          "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
          "abc (one block)");

    // FIPS 180-4 B.2: 56-char input spanning block/padding boundaries.
    check("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
          "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
          "56-char string");

    // Empty string (well-known).
    check("",
          "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          "empty");

    // One million 'a' characters (FIPS 180-4 B.3).
    check(std::string(1000000, 'a'),
          "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0",
          "1,000,000 a's");

    // Boundary consistency: feed each length 1..130 one byte at a time and
    // compare the streamed digest with the one-shot digest. This exercises
    // every block boundary and every padding length with a verifiable
    // invariant rather than a hard-coded constant.
    for (int len = 1; len <= 130; ++len) {
        std::string s(len, 'x');
        Sha256 h;
        for (int i = 0; i < len; ++i) {
            uint8_t b = static_cast<uint8_t>('x');
            h.update(&b, 1);
        }
        const std::string streamed = toHex(h.finish());
        const std::string oneshot = Sha256::hex(s);
        if (streamed != oneshot) {
            std::printf("FAIL stream/oneshot mismatch at len=%d\n", len);
            ++failures;
        }
    }
    std::printf("ok   streaming matches one-shot for lengths 1..130\n");

    // Mixed chunk splits must be chunk-boundary independent.
    {
        std::string s;
        for (int i = 0; i < 1000; ++i) s.push_back(char('A' + i % 26));
        const std::string ref = Sha256::hex(s);
        Sha256 h;
        size_t off = 0;
        const size_t chunks[] = {1, 7, 63, 64, 65, 100};
        int ci = 0;
        while (off < s.size()) {
            size_t n = std::min(chunks[ci++ % 6], s.size() - off);
            h.update(s.data() + off, n);
            off += n;
        }
        if (toHex(h.finish()) != ref) {
            std::printf("FAIL irregular chunking mismatch\n");
            ++failures;
        } else {
            std::printf("ok   irregular chunk splits\n");
        }
    }

    if (failures) {
        std::printf("\n%d SHA-256 KAT(s) FAILED\n", failures);
        return 1;
    }
    std::printf("\nAll SHA-256 tests passed.\n");
    return 0;
}
