#pragma once
// Real cryptographic operations via OpenSSL 3 (EVP): SHA-256 digests and
// HMAC-SHA-256 signatures. No hand-rolled crypto.
#include <string>

namespace pgo {

// Lowercase hex SHA-256 of the bytes in `data`.
std::string Sha256Hex(const std::string& data);

// Lowercase hex HMAC-SHA-256(key, data). Empty key is an error.
bool HmacSha256Hex(const std::string& key, const std::string& data,
                   std::string* out, std::string* err);

}  // namespace pgo
