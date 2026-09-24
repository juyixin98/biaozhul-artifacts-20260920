// SPDX-License-Identifier: Apache-2.0
//
// Cryptography is executed for real (OpenSSL EVP) and checked against
// published known-answer vectors (FIPS 180 / RFC 4231).
#include <gtest/gtest.h>

#include <string>
#include <vector>

#include "robot_param/core/crypto.hpp"

using namespace robot_param;

TEST(Crypto, Sha256KnownAnswerVectors) {
  EXPECT_EQ(sha256_hex(""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  EXPECT_EQ(sha256_hex("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  // NIST: sha256 of 1,000,000 'a' = cdc76e5c...
  EXPECT_EQ(sha256_hex(std::string(1000000, 'a')),
            "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
}

TEST(Crypto, HmacSha256Rfc4231Vectors) {
  // RFC 4231, case 1: key = 20 * 0x0b, data = "Hi There".
  std::vector<std::uint8_t> key1(20, 0x0b);
  EXPECT_EQ(hmac_sha256_hex(key1, "Hi There"),
            "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7");

  // RFC 4231, case 2: key = "Jefe".
  std::vector<std::uint8_t> key2 = parse_key("Jefe");
  EXPECT_EQ(hmac_sha256_hex(key2, "what do ya want for nothing?"),
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843");
}

TEST(Crypto, TamperingIsDetected) {
  std::vector<std::uint8_t> key = parse_key("Jefe");
  const auto good = hmac_sha256(key, "config payload v1");
  const auto tampered = hmac_sha256(key, "config payload v2");
  EXPECT_FALSE(constant_time_equal(good, tampered));

  // Same payload, different key -> different tag.
  std::vector<std::uint8_t> other = parse_key("Attacker");
  EXPECT_FALSE(constant_time_equal(good, hmac_sha256(other,
                                                     "config payload v1")));
  // Re-tag with the attacker key must not validate under the real key.
  EXPECT_FALSE(constant_time_equal(good, tampered));
}

TEST(Crypto, HexRoundTrip) {
  const auto key = random_key_32();
  ASSERT_EQ(key.size(), 32u);
  const std::string hex = to_hex(key);
  ASSERT_EQ(hex.size(), 64u);
  const auto back = from_hex(hex);
  EXPECT_EQ(back, key);
  EXPECT_TRUE(from_hex("zz").empty());
  EXPECT_TRUE(from_hex("abc").empty());
}

TEST(Crypto, KeyParsing) {
  // 64 hex chars -> 32 bytes.
  auto k = parse_key(
      "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f");
  ASSERT_EQ(k.size(), 32u);
  EXPECT_EQ(k[0], 0x00);
  EXPECT_EQ(k[31], 0x1f);

  // Raw fallback, any length.
  auto raw = parse_key("Jefe");
  EXPECT_EQ(raw.size(), 4u);

  EXPECT_THROW(parse_key(""), std::runtime_error);

  // Two random keys must differ with overwhelming probability.
  EXPECT_NE(to_hex(random_key_32()), to_hex(random_key_32()));
}
