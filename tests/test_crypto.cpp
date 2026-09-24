#include "crypto.hpp"

#include "test_util.hpp"

#include <set>
#include <string>

using namespace pf;

static void sha256_known_vectors() {
    // NIST FIPS 180-2 vectors.
    CHECK(crypto::sha256_hex("") ==
          "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    CHECK(crypto::sha256_hex("abc") ==
          "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    std::string million(1000000, 'a');
    CHECK(crypto::sha256_hex(million) ==
          "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
}

static void hmac_rfc4231() {
    // RFC 4231 test case 1 (key = 20 x 0x0b, data = "Hi There").
    std::string key(20, '\x0b');
    CHECK(crypto::hmac_sha256_hex(key, "Hi There") ==
          "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7");
    // Case 2.
    CHECK(crypto::hmac_sha256_hex("Jefe",
                                  "what do ya want for nothing?") ==
          "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843");
    // Tampered data must change the MAC.
    CHECK(crypto::hmac_sha256_hex("Jefe", "what do ya want for nothing") !=
          "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843");
}

static void b64url_roundtrip() {
    for (size_t n : {size_t(0), size_t(1), size_t(2), size_t(3),
                     size_t(16), size_t(31), size_t(32), size_t(100)}) {
        auto bytes = crypto::secure_random_bytes(n);
        std::string s = crypto::b64url_encode(bytes);
        CHECK(s.find('+') == std::string::npos);
        CHECK(s.find('/') == std::string::npos);
        CHECK(s.find('=') == std::string::npos);
        auto back = crypto::b64url_decode(s);
        CHECK(back == bytes);
    }
    // Known vector: "Man" -> "TWFu" in classic base64.
    std::vector<unsigned char> man{'M', 'a', 'n'};
    CHECK(crypto::b64url_encode(man) == "TWFu");
}

static void random_is_random_and_fixed_size() {
    auto a = crypto::secure_random_bytes(32);
    auto b = crypto::secure_random_bytes(32);
    CHECK(a.size() == 32 && b.size() == 32);
    CHECK(a != b);
    std::set<std::string> uniq;
    for (int i = 0; i < 100; ++i)
        uniq.insert(crypto::b64url_encode(crypto::secure_random_bytes(8)));
    CHECK(uniq.size() == 100);
}

static void constant_time_comparison() {
    CHECK(crypto::constant_time_eq("same", "same"));
    CHECK(!crypto::constant_time_eq("same", "diff"));
    CHECK(!crypto::constant_time_eq("short", "longer"));
    std::string mac(64, 'a');
    CHECK(crypto::constant_time_eq(mac, std::string(64, 'a')));
    std::string tampered = mac;
    tampered[63] = 'b';
    CHECK(!crypto::constant_time_eq(mac, tampered));
}

int main() {
    RUN_TEST(sha256_known_vectors);
    RUN_TEST(hmac_rfc4231);
    RUN_TEST(b64url_roundtrip);
    RUN_TEST(random_is_random_and_fixed_size);
    RUN_TEST(constant_time_comparison);
    return TEST_MAIN_RETURN();
}
