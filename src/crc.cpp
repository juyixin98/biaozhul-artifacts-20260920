// SPDX-License-Identifier: MIT
#include "shmrq/ring.h"

#include <array>
#include <cstring>

namespace shmrq {
namespace {

struct CrcTable {
  std::array<uint32_t, 256> v{};
  constexpr CrcTable() {
    // CRC-32/ISO-HDLC (IEEE 802.3): poly 0xEDB88320 reflected,
    // init/xorout 0xFFFFFFFF.  check("123456789") == 0xCBF43926.
    for (uint32_t i = 0; i < 256; ++i) {
      uint32_t c = i;
      for (int k = 0; k < 8; ++k)
        c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
      v[i] = c;
    }
  }
};
constexpr CrcTable kTable{};

uint32_t crc_update(uint32_t crc, const void* p, size_t n) noexcept {
  const auto* b = static_cast<const uint8_t*>(p);
  for (size_t i = 0; i < n; ++i)
    crc = kTable.v[(crc ^ b[i]) & 0xFFu] ^ (crc >> 8);
  return crc;
}

} // namespace

uint32_t crc32_ieee(const void* data, size_t len) noexcept {
  uint32_t crc = 0xFFFFFFFFu;
  if (len && data)
    crc = crc_update(crc, data, len);
  return crc ^ 0xFFFFFFFFu;
}

uint32_t Ring::crc_record(uint64_t seq, const void* data, uint32_t len) noexcept {
  // Integrity domain = little-endian(seq) || little-endian(len) || payload.
  // Folding the position in means a valid record copied into another slot
  // fails verification (no cross-slot replay/swap confusion).
  uint8_t le[8 + 4];
  for (int i = 0; i < 8; ++i)
    le[i] = static_cast<uint8_t>((seq >> (8 * i)) & 0xFF);
  uint32_t l = len;
  for (int i = 0; i < 4; ++i)
    le[8 + i] = static_cast<uint8_t>((l >> (8 * i)) & 0xFF);
  uint32_t crc = 0xFFFFFFFFu;
  crc = crc_update(crc, le, sizeof(le));
  if (len && data)
    crc = crc_update(crc, data, len);
  return crc ^ 0xFFFFFFFFu;
}

} // namespace shmrq
