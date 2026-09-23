// SHA-256 (FIPS 180-4). Based on the public-domain reference implementation
// by Brad Conte (brad@bradconte.com), with C++ wrappers added.
#ifndef PGO_SHA256_HPP
#define PGO_SHA256_HPP

#include <cstddef>
#include <cstdint>
#include <string>

namespace pgo {

struct Sha256Ctx {
  uint8_t data[64];
  uint32_t datalen = 0;
  uint64_t bitlen = 0;
  uint32_t state[8];
};

void sha256Init(Sha256Ctx* ctx);
void sha256Update(Sha256Ctx* ctx, const uint8_t* data, size_t len);
void sha256Final(Sha256Ctx* ctx, uint8_t hash[32]);

// One-shot hex digest of a byte buffer / string (lowercase hex, 64 chars).
std::string sha256Hex(const std::string& input);
std::string toHex(const uint8_t* data, size_t len);

}  // namespace pgo

#endif
