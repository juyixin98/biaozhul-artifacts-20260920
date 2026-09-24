// crypto.hpp — real cryptographic operations via OpenSSL libcrypto (no shims).
#pragma once

#include <string>

namespace jtp {

// Hex-encoded SHA-256 digest of the raw bytes, computed through the
// OpenSSL 3 EVP message-digest API.
std::string Sha256Hex(const std::string& data);

// Constant-time comparison (CRYPTO_memcmp wrapper) for digests/tokens.
bool ConstantTimeEquals(const std::string& a, const std::string& b);

}  // namespace jtp
