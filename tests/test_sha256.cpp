// SHA-256 against FIPS 180-4 / NIST CAVP known-answer vectors.
#include <cstdio>
#include <string>

#include "gridfusion/sha256.hpp"

using gridfusion::sha256::hex;
using gridfusion::sha256::Sha256;

static int failures = 0;

static void check(const std::string& got, const std::string& want,
                  const char* name) {
    if (got != want) {
        std::printf("FAIL %s\n  got  %s\n  want %s\n", name, got.c_str(),
                    want.c_str());
        ++failures;
    } else {
        std::printf("ok   %s\n", name);
    }
}

int main() {
    check(hex(""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b785"
                   "2b855",
          "empty string");
    check(hex("abc"),
          "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
          "abc");
    check(hex("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
          "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
          "56-byte two-block message");
    check(hex("The quick brown fox jumps over the lazy dog"),
          "d7a8fbb307d7809469ca9abcb0082e4f8d5651e46d3cdb762d02d0bf37c9e592",
          "pangram");

    // Streaming interface must match one-shot, including a 1-byte split.
    {
        Sha256 h;
        const std::string m = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmn"
                              "lmnomnopnopq";
        for (char c : m)
            h.update(reinterpret_cast<const unsigned char*>(&c), 1);
        check(h.finish().hex(),
              "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db"
              "06c1",
              "streamed byte-at-a-time");
    }

    // 'a' repeated 1,000,000 times (NIST long message vector).
    {
        Sha256 h;
        std::string chunk(1000, 'a');
        for (int i = 0; i < 1000; ++i) h.update(chunk);
        check(h.finish().hex(),
              "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112"
              "cd0",
              "1e6 'a's");
    }

    if (failures) {
        std::printf("%d SHA-256 check(s) failed\n", failures);
        return 1;
    }
    std::printf("all SHA-256 vectors passed\n");
    return 0;
}
