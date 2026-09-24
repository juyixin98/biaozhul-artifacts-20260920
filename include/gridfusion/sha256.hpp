#pragma once
// Real FIPS 180-4 SHA-256 implementation (no OpenSSL dependency).
//
// Usage:
//   std::string digest = gridfusion::sha256::hex(data);
//
// The same routine is used for the dependency lock verification note in the
// README and, more importantly, for binding a map version id to its geometry
// (resolution + origin + size), so map-version ids are real cryptographic
// hashes of a canonical spec string.

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>

namespace gridfusion::sha256 {

struct Digest {
    std::uint8_t bytes[32]{};
    std::string hex() const;
    bool operator==(const Digest&) const;
};

class Sha256 {
public:
    Sha256();
    void update(const std::uint8_t* data, std::size_t len);
    void update(std::string_view s);
    Digest finish();

private:
    void transform(const std::uint8_t block[64]);

    std::uint32_t state_[8];
    std::uint64_t bitlen_ = 0;
    std::uint8_t buffer_[64]{};
    std::size_t buflen_ = 0;
    bool finished_ = false;
};

Digest of(std::string_view s);
std::string hex(std::string_view s);

}  // namespace gridfusion::sha256
