#include "crypto.h"

#include <openssl/evp.h>
#include <openssl/hmac.h>

#include <array>
#include <cstdio>

namespace pgo {
namespace {

std::string ToHex(const unsigned char* p, size_t n) {
  static const char* h = "0123456789abcdef";
  std::string out(n * 2, '0');
  for (size_t i = 0; i < n; ++i) {
    out[2 * i] = h[p[i] >> 4];
    out[2 * i + 1] = h[p[i] & 0xF];
  }
  return out;
}

}  // namespace

std::string Sha256Hex(const std::string& data) {
  unsigned char md[EVP_MAX_MD_SIZE];
  size_t md_len = 0;
  EVP_Q_digest(nullptr, "SHA256", nullptr, data.data(), data.size(), md,
               &md_len);
  return ToHex(md, md_len);
}

bool HmacSha256Hex(const std::string& key, const std::string& data,
                   std::string* out, std::string* err) {
  if (key.empty()) {
    if (err) *err = "empty HMAC key";
    return false;
  }
  unsigned char md[EVP_MAX_MD_SIZE];
  unsigned int md_len = 0;
  unsigned char* rc = HMAC(EVP_sha256(), key.data(),
                           static_cast<int>(key.size()),
                           reinterpret_cast<const unsigned char*>(data.data()),
                           data.size(), md, &md_len);
  if (rc == nullptr) {
    if (err) *err = "HMAC computation failed";
    return false;
  }
  *out = ToHex(md, md_len);
  return true;
}

}  // namespace pgo
