// SHA-256 (FIPS 180-4), self-contained real cryptographic implementation.
// Used so the service can prove it genuinely processed the request bytes:
// every registration response includes the SHA-256 of the raw request body.
#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <string_view>

namespace pcrs {

class Sha256 {
public:
    Sha256();
    void update(const uint8_t* data, size_t len);
    void update(std::string_view data) {
        update(reinterpret_cast<const uint8_t*>(data.data()), data.size());
    }
    std::array<uint8_t, 32> finish();

    static std::array<uint8_t, 32> hash(const uint8_t* data, size_t len) {
        Sha256 h;
        h.update(data, len);
        return h.finish();
    }
    static std::string hex(std::string_view data);

private:
    void process_block(const uint8_t* block);

    uint32_t state_[8]{};
    uint8_t buffer_[64]{};
    size_t buf_len_ = 0;
    uint64_t total_len_ = 0;
};

}  // namespace pcrs
