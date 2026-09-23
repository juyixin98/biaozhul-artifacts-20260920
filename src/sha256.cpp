#include "sha256.hpp"

#include <cstring>

namespace pgo {

namespace {

#define ROTRIGHT(a, b) (((a) >> (b)) | ((a) << (32 - (b))))
#define EP0(x) (ROTRIGHT(x, 2) ^ ROTRIGHT(x, 13) ^ ROTRIGHT(x, 22))
#define EP1(x) (ROTRIGHT(x, 6) ^ ROTRIGHT(x, 11) ^ ROTRIGHT(x, 25))
#define SIG0(x) (ROTRIGHT(x, 7) ^ ROTRIGHT(x, 18) ^ ((x) >> 3))
#define SIG1(x) (ROTRIGHT(x, 17) ^ ROTRIGHT(x, 19) ^ ((x) >> 10))

constexpr uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};

void sha256Transform(uint32_t state[8], const uint8_t data[64]) {
  uint32_t a, b, c, d, e, f, g, h, i, j, t1, t2, m[64];

  for (i = 0, j = 0; i < 16; ++i, j += 4) {
    m[i] = (static_cast<uint32_t>(data[j]) << 24) | (static_cast<uint32_t>(data[j + 1]) << 16) |
           (static_cast<uint32_t>(data[j + 2]) << 8) | (static_cast<uint32_t>(data[j + 3]));
  }
  for (; i < 64; ++i) {
    m[i] = SIG1(m[i - 2]) + m[i - 7] + SIG0(m[i - 15]) + m[i - 16];
  }

  a = state[0]; b = state[1]; c = state[2]; d = state[3];
  e = state[4]; f = state[5]; g = state[6]; h = state[7];

  for (i = 0; i < 64; ++i) {
    t1 = h + EP1(e) + ((e & f) ^ (~e & g)) + K[i] + m[i];
    t2 = EP0(a) + ((a & b) ^ (a & c) ^ (b & c));
    h = g; g = f; f = e; e = d + t1;
    d = c; c = b; b = a; a = t1 + t2;
  }

  state[0] += a; state[1] += b; state[2] += c; state[3] += d;
  state[4] += e; state[5] += f; state[6] += g; state[7] += h;
}

}  // namespace

void sha256Init(Sha256Ctx* ctx) {
  ctx->datalen = 0;
  ctx->bitlen = 0;
  ctx->state[0] = 0x6a09e667;
  ctx->state[1] = 0xbb67ae85;
  ctx->state[2] = 0x3c6ef372;
  ctx->state[3] = 0xa54ff53a;
  ctx->state[4] = 0x510e527f;
  ctx->state[5] = 0x9b05688c;
  ctx->state[6] = 0x1f83d9ab;
  ctx->state[7] = 0x5be0cd19;
}

void sha256Update(Sha256Ctx* ctx, const uint8_t* data, size_t len) {
  for (size_t i = 0; i < len; ++i) {
    ctx->data[ctx->datalen++] = data[i];
    if (ctx->datalen == 64) {
      sha256Transform(ctx->state, ctx->data);
      ctx->bitlen += 512;
      ctx->datalen = 0;
    }
  }
}

void sha256Final(Sha256Ctx* ctx, uint8_t hash[32]) {
  uint32_t i = ctx->datalen;

  // Pad whatever data is left in the buffer.
  if (ctx->datalen < 56) {
    ctx->data[i++] = 0x80;
    while (i < 56) ctx->data[i++] = 0x00;
  } else {
    ctx->data[i++] = 0x80;
    while (i < 64) ctx->data[i++] = 0x00;
    sha256Transform(ctx->state, ctx->data);
    std::memset(ctx->data, 0, 56);
  }

  ctx->bitlen += static_cast<uint64_t>(ctx->datalen) * 8;
  ctx->data[63] = static_cast<uint8_t>(ctx->bitlen);
  ctx->data[62] = static_cast<uint8_t>(ctx->bitlen >> 8);
  ctx->data[61] = static_cast<uint8_t>(ctx->bitlen >> 16);
  ctx->data[60] = static_cast<uint8_t>(ctx->bitlen >> 24);
  ctx->data[59] = static_cast<uint8_t>(ctx->bitlen >> 32);
  ctx->data[58] = static_cast<uint8_t>(ctx->bitlen >> 40);
  ctx->data[57] = static_cast<uint8_t>(ctx->bitlen >> 48);
  ctx->data[56] = static_cast<uint8_t>(ctx->bitlen >> 56);
  sha256Transform(ctx->state, ctx->data);

  for (i = 0; i < 4; ++i) {
    hash[i] = static_cast<uint8_t>((ctx->state[0] >> (24 - i * 8)) & 0xFF);
    hash[i + 4] = static_cast<uint8_t>((ctx->state[1] >> (24 - i * 8)) & 0xFF);
    hash[i + 8] = static_cast<uint8_t>((ctx->state[2] >> (24 - i * 8)) & 0xFF);
    hash[i + 12] = static_cast<uint8_t>((ctx->state[3] >> (24 - i * 8)) & 0xFF);
    hash[i + 16] = static_cast<uint8_t>((ctx->state[4] >> (24 - i * 8)) & 0xFF);
    hash[i + 20] = static_cast<uint8_t>((ctx->state[5] >> (24 - i * 8)) & 0xFF);
    hash[i + 24] = static_cast<uint8_t>((ctx->state[6] >> (24 - i * 8)) & 0xFF);
    hash[i + 28] = static_cast<uint8_t>((ctx->state[7] >> (24 - i * 8)) & 0xFF);
  }
}

std::string toHex(const uint8_t* data, size_t len) {
  static const char* d = "0123456789abcdef";
  std::string out;
  out.resize(len * 2);
  for (size_t i = 0; i < len; ++i) {
    out[2 * i] = d[data[i] >> 4];
    out[2 * i + 1] = d[data[i] & 0xF];
  }
  return out;
}

std::string sha256Hex(const std::string& input) {
  Sha256Ctx ctx;
  sha256Init(&ctx);
  sha256Update(&ctx, reinterpret_cast<const uint8_t*>(input.data()), input.size());
  uint8_t hash[32];
  sha256Final(&ctx, hash);
  return toHex(hash, 32);
}

}  // namespace pgo
