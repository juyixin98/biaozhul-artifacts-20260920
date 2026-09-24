#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace tf {

// Computes HMAC-SHA256(key, message) using the OpenSSL 3 EVP_MAC API.
// Returns 32 raw bytes; throws std::runtime_error on crypto failure.
std::vector<uint8_t> hmacSha256(const std::string& key,
                                const std::string& message);

// Lowercase hex encoding.
std::string toHex(const std::vector<uint8_t>& bytes);

// HMAC-SHA256 as lowercase hex (64 chars).
std::string hmacSha256Hex(const std::string& key, const std::string& message);

// Constant-time string equality; false on length mismatch.
bool constantTimeEquals(const std::string& a, const std::string& b);

}  // namespace tf
