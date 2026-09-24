#include "crypto.hpp"

#include <openssl/evp.h>
#include <openssl/core_names.h>
#include <openssl/params.h>

#include <array>
#include <memory>
#include <stdexcept>

namespace tf {

namespace {
struct MacCtxDeleter {
  void operator()(EVP_MAC_CTX* p) const { EVP_MAC_CTX_free(p); }
};
struct MacDeleter {
  void operator()(EVP_MAC* p) const { EVP_MAC_free(p); }
};
}  // namespace

std::vector<uint8_t> hmacSha256(const std::string& key,
                                const std::string& message) {
  std::unique_ptr<EVP_MAC, MacDeleter> mac(EVP_MAC_fetch(nullptr, "HMAC", nullptr));
  if (!mac) throw std::runtime_error("EVP_MAC_fetch(HMAC) failed");

  std::unique_ptr<EVP_MAC_CTX, MacCtxDeleter> ctx(EVP_MAC_CTX_new(mac.get()));
  if (!ctx) throw std::runtime_error("EVP_MAC_CTX_new failed");

  // OSSL_MAC_PARAM_DIGEST = "digest"
  std::array<OSSL_PARAM, 2> params{};
  params[0] = OSSL_PARAM_construct_utf8_string(
      OSSL_MAC_PARAM_DIGEST, const_cast<char*>("SHA256"), 0);
  params[1] = OSSL_PARAM_construct_end();

  if (EVP_MAC_init(ctx.get(),
                   reinterpret_cast<const unsigned char*>(key.data()),
                   key.size(), params.data()) != 1)
    throw std::runtime_error("EVP_MAC_init failed");
  if (EVP_MAC_update(
          ctx.get(),
          reinterpret_cast<const unsigned char*>(message.data()),
          message.size()) != 1)
    throw std::runtime_error("EVP_MAC_update failed");

  std::vector<uint8_t> out(EVP_MAX_MD_SIZE);
  size_t out_len = 0;
  if (EVP_MAC_final(ctx.get(), out.data(), &out_len, out.size()) != 1)
    throw std::runtime_error("EVP_MAC_final failed");
  out.resize(out_len);
  return out;
}

std::string toHex(const std::vector<uint8_t>& bytes) {
  static const char* k = "0123456789abcdef";
  std::string s;
  s.reserve(bytes.size() * 2);
  for (uint8_t b : bytes) {
    s.push_back(k[b >> 4]);
    s.push_back(k[b & 0xF]);
  }
  return s;
}

std::string hmacSha256Hex(const std::string& key,
                          const std::string& message) {
  return toHex(hmacSha256(key, message));
}

bool constantTimeEquals(const std::string& a, const std::string& b) {
  if (a.size() != b.size()) return false;
  unsigned char diff = 0;
  for (size_t i = 0; i < a.size(); ++i)
    diff |= static_cast<unsigned char>(a[i] ^ b[i]);
  return diff == 0;
}

}  // namespace tf
