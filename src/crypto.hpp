// Cryptographic primitives backed by OpenSSL libcrypto:
//  - SHA-256
//  - HMAC-SHA-256
//  - cryptographically secure random bytes (gen-secret / map ids / nonces)
//  - constant-time byte comparison
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace pf::crypto {

std::string sha256_hex(const std::string& data);
// Raw 32-byte digest.
std::vector<std::uint8_t> sha256(const std::string& data);

std::string hmac_sha256_hex(const std::string& key, const std::string& data);

// URL-safe base64 (no padding), used for map ids and secrets.
std::string b64url_encode(const std::vector<std::uint8_t>& bytes);
std::vector<std::uint8_t> b64url_decode(const std::string& s);

// n bytes from a CSPRNG (RAND_bytes); throws std::runtime_error on failure.
std::vector<std::uint8_t> secure_random_bytes(size_t n);

// Constant-time equality (lengths differing returns false without leaking
// which position differed, beyond length itself).
bool constant_time_eq(const std::string& a, const std::string& b);

}  // namespace pf::crypto
