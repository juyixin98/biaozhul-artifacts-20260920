// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace robot_param {

// Hex helpers (lower-case hex, used for SHA-256 / HMAC digests).
std::string to_hex(const std::vector<std::uint8_t>& bytes);
// Returns empty vector on malformed input (wrong length / non-hex chars).
std::vector<std::uint8_t> from_hex(const std::string& hex);

// Constant-time equality for digests.
bool constant_time_equal(const std::vector<std::uint8_t>& a,
                         const std::vector<std::uint8_t>& b);

// SHA-256 via OpenSSL EVP (real cryptographic hash, never a hand-rolled one).
// Throws std::runtime_error only on an internal OpenSSL failure.
std::vector<std::uint8_t> sha256(const std::string& message);
std::string sha256_hex(const std::string& message);

// HMAC-SHA-256 via the OpenSSL EVP_MAC API.
// `key` may be raw bytes of ANY length (recommended 32), or a 64-character hex
// string encoding 32 bytes (handy for environment variables / examples).
std::vector<std::uint8_t> hmac_sha256(const std::vector<std::uint8_t>& key,
                                      const std::string& message);
std::string hmac_sha256_hex(const std::vector<std::uint8_t>& key,
                            const std::string& message);

// Parse a key provided as raw bytes or 64-hex. Empty input is an error.
std::vector<std::uint8_t> parse_key(const std::string& key_material);

// Fill 32 cryptographically secure random bytes (RAND_bytes).
std::vector<std::uint8_t> random_key_32();

}  // namespace robot_param
