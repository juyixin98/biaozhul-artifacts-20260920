// SPDX-License-Identifier: Apache-2.0
#include "robot_param/core/crypto.hpp"

#include <openssl/crypto.h>
#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/params.h>
#include <openssl/rand.h>

#include <array>
#include <stdexcept>

namespace robot_param {

namespace {

std::string openssl_error() {
  unsigned long code = ERR_peek_last_error();
  if (code == 0) {
    return "unknown OpenSSL error";
  }
  char buf[256] = {};
  ERR_error_string_n(code, buf, sizeof(buf));
  return std::string(buf);
}

}  // namespace

std::string to_hex(const std::vector<std::uint8_t>& bytes) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string out;
  out.reserve(bytes.size() * 2);
  for (std::uint8_t b : bytes) {
    out.push_back(kHex[b >> 4]);
    out.push_back(kHex[b & 0x0f]);
  }
  return out;
}

std::vector<std::uint8_t> from_hex(const std::string& hex) {
  if (hex.size() % 2 != 0) {
    return {};
  }
  auto nibble = [](char c, int& n) -> bool {
    if (c >= '0' && c <= '9') {
      n = c - '0';
    } else if (c >= 'a' && c <= 'f') {
      n = c - 'a' + 10;
    } else if (c >= 'A' && c <= 'F') {
      n = c - 'A' + 10;
    } else {
      return false;
    }
    return true;
  };
  std::vector<std::uint8_t> out;
  out.reserve(hex.size() / 2);
  for (std::size_t i = 0; i < hex.size(); i += 2) {
    int hi = 0, lo = 0;
    if (!nibble(hex[i], hi) || !nibble(hex[i + 1], lo)) {
      return {};
    }
    out.push_back(static_cast<std::uint8_t>((hi << 4) | lo));
  }
  return out;
}

bool constant_time_equal(const std::vector<std::uint8_t>& a,
                         const std::vector<std::uint8_t>& b) {
  if (a.size() != b.size()) {
    return false;
  }
  return CRYPTO_memcmp(a.data(), b.data(), a.size()) == 0;
}

std::vector<std::uint8_t> sha256(const std::string& message) {
  EVP_MD_CTX* ctx = EVP_MD_CTX_new();
  if (ctx == nullptr) {
    throw std::runtime_error("EVP_MD_CTX_new failed: " + openssl_error());
  }
  std::vector<std::uint8_t> digest(EVP_MAX_MD_SIZE);
  unsigned int digest_len = 0;
  try {
    if (EVP_DigestInit_ex(ctx, EVP_sha256(), nullptr) != 1 ||
        EVP_DigestUpdate(ctx, message.data(), message.size()) != 1 ||
        EVP_DigestFinal_ex(ctx, digest.data(), &digest_len) != 1) {
      throw std::runtime_error("SHA-256 computation failed: " + openssl_error());
    }
  } catch (...) {
    EVP_MD_CTX_free(ctx);
    throw;
  }
  EVP_MD_CTX_free(ctx);
  digest.resize(digest_len);
  return digest;
}

std::string sha256_hex(const std::string& message) {
  return to_hex(sha256(message));
}

std::vector<std::uint8_t> hmac_sha256(const std::vector<std::uint8_t>& key,
                                      const std::string& message) {
  if (key.empty()) {
    throw std::runtime_error("HMAC key must not be empty");
  }

  EVP_MAC* mac = EVP_MAC_fetch(nullptr, "HMAC", nullptr);
  if (mac == nullptr) {
    throw std::runtime_error("EVP_MAC_fetch(HMAC) failed: " + openssl_error());
  }
  EVP_MAC_CTX* ctx = EVP_MAC_CTX_new(mac);
  EVP_MAC_free(mac);
  if (ctx == nullptr) {
    throw std::runtime_error("EVP_MAC_CTX_new failed: " + openssl_error());
  }

  std::array<OSSL_PARAM, 2> params{};
  params[0] = OSSL_PARAM_construct_utf8_string(
      "digest", const_cast<char*>("SHA256"), 0);
  params[1] = OSSL_PARAM_construct_end();

  std::vector<std::uint8_t> out(EVP_MAX_MD_SIZE);
  std::size_t out_len = 0;
  try {
    if (EVP_MAC_init(ctx, key.data(), key.size(), params.data()) != 1 ||
        EVP_MAC_update(ctx, reinterpret_cast<const unsigned char*>(message.data()),
                       message.size()) != 1 ||
        EVP_MAC_final(ctx, out.data(), &out_len, out.size()) != 1) {
      throw std::runtime_error("HMAC-SHA-256 computation failed: " +
                               openssl_error());
    }
  } catch (...) {
    EVP_MAC_CTX_free(ctx);
    throw;
  }
  EVP_MAC_CTX_free(ctx);
  out.resize(out_len);
  return out;
}

std::string hmac_sha256_hex(const std::vector<std::uint8_t>& key,
                            const std::string& message) {
  return to_hex(hmac_sha256(key, message));
}

std::vector<std::uint8_t> parse_key(const std::string& material) {
  if (material.empty()) {
    throw std::runtime_error("HMAC key material is empty");
  }
  // 64 hex chars -> 32 raw bytes. Anything else is treated as raw bytes
  // (any length accepted, 32 bytes recommended).
  if (material.size() == 64) {
    std::vector<std::uint8_t> raw = from_hex(material);
    if (!raw.empty()) {
      return raw;
    }
  }
  return std::vector<std::uint8_t>(material.begin(), material.end());
}

std::vector<std::uint8_t> random_key_32() {
  std::vector<std::uint8_t> key(32);
  if (RAND_bytes(key.data(), static_cast<int>(key.size())) != 1) {
    throw std::runtime_error("RAND_bytes failed: " + openssl_error());
  }
  return key;
}

}  // namespace robot_param
