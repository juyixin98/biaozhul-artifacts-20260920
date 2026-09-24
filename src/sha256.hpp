// sha256.hpp —— FIPS 180-4 SHA-256 真实实现（无外部依赖）
#pragma once

#include <array>
#include <cstdint>
#include <cstddef>
#include <string>
#include <string_view>

namespace sha256 {

using Digest = std::array<uint8_t, 32>;

class Sha256 {
public:
    Sha256() { reset(); }
    void reset();
    void update(const uint8_t* data, std::size_t len);
    void update(std::string_view s) {
        update(reinterpret_cast<const uint8_t*>(s.data()), s.size());
    }
    Digest finish();

private:
    void processBlock(const uint8_t* block);
    std::array<uint32_t, 8> state_{};
    uint64_t bitlen_ = 0;
    std::array<uint8_t, 64> buffer_{};
    std::size_t buflen_ = 0;
};

Digest hash(std::string_view s);
std::string toHex(const Digest& d);
std::string hex(std::string_view s);

}  // namespace sha256
