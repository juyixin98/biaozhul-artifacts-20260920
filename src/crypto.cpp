#include "crypto.hpp"

#include <openssl/crypto.h>
#include <openssl/evp.h>

#include <array>
#include <stdexcept>

namespace jtp {

std::string Sha256Hex(const std::string& data) {
  unsigned char digest[EVP_MAX_MD_SIZE];
  unsigned int digest_len = 0;

  EVP_MD_CTX* ctx = EVP_MD_CTX_new();
  if (ctx == nullptr) {
    throw std::runtime_error("EVP_MD_CTX_new failed");
  }
  try {
    if (EVP_DigestInit_ex(ctx, EVP_sha256(), nullptr) != 1 ||
        EVP_DigestUpdate(ctx, data.data(), data.size()) != 1 ||
        EVP_DigestFinal_ex(ctx, digest, &digest_len) != 1) {
      EVP_MD_CTX_free(ctx);
      throw std::runtime_error("SHA-256 computation failed");
    }
  } catch (...) {
    EVP_MD_CTX_free(ctx);
    throw;
  }
  EVP_MD_CTX_free(ctx);

  static const char kHex[] = "0123456789abcdef";
  std::string out;
  out.resize(digest_len * 2);
  for (unsigned int i = 0; i < digest_len; ++i) {
    out[2 * i] = kHex[digest[i] >> 4];
    out[2 * i + 1] = kHex[digest[i] & 0x0F];
  }
  return out;
}

bool ConstantTimeEquals(const std::string& a, const std::string& b) {
  if (a.size() != b.size()) return false;
  return CRYPTO_memcmp(a.data(), b.data(), a.size()) == 0;
}

}  // namespace jtp
