#include "crypto.hpp"

#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/rand.h>

#include <array>
#include <stdexcept>

namespace pf::crypto {

namespace {

const char* kHex = "0123456789abcdef";

std::string to_hex(const std::uint8_t* p, size_t n) {
    std::string out;
    out.resize(n * 2);
    for (size_t i = 0; i < n; ++i) {
        out[2 * i] = kHex[p[i] >> 4];
        out[2 * i + 1] = kHex[p[i] & 0x0f];
    }
    return out;
}

// EVP one-shot helper; throws on any failure so callers never proceed with
// partial/undefined cryptographic output.
std::vector<std::uint8_t> evp_digest(const EVP_MD* md, const std::string& data) {
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    if (!ctx) throw std::runtime_error("EVP_MD_CTX_new failed");
    std::vector<std::uint8_t> out(EVP_MAX_MD_SIZE);
    unsigned int out_len = 0;
    if (EVP_DigestInit_ex(ctx, md, nullptr) != 1 ||
        EVP_DigestUpdate(ctx, data.data(), data.size()) != 1 ||
        EVP_DigestFinal_ex(ctx, out.data(), &out_len) != 1) {
        EVP_MD_CTX_free(ctx);
        throw std::runtime_error("SHA-256 computation failed");
    }
    EVP_MD_CTX_free(ctx);
    out.resize(out_len);
    return out;
}

}  // namespace

std::vector<std::uint8_t> sha256(const std::string& data) {
    return evp_digest(EVP_sha256(), data);
}

std::string sha256_hex(const std::string& data) {
    auto d = sha256(data);
    return to_hex(d.data(), d.size());
}

std::string hmac_sha256_hex(const std::string& key, const std::string& data) {

    unsigned int out_len = EVP_MAX_MD_SIZE;
    std::array<std::uint8_t, EVP_MAX_MD_SIZE> out{};
    unsigned char* rc = HMAC(
        EVP_sha256(),
        key.data(), static_cast<int>(key.size()),
        reinterpret_cast<const unsigned char*>(data.data()), data.size(),
        out.data(), &out_len);
    if (rc == nullptr) {
        throw std::runtime_error("HMAC-SHA256 computation failed");
    }
    return to_hex(out.data(), out_len);
}

std::vector<std::uint8_t> secure_random_bytes(size_t n) {
    std::vector<std::uint8_t> bytes(n);
    if (RAND_bytes(bytes.data(), static_cast<int>(n)) != 1) {
        throw std::runtime_error("RAND_bytes failed: CSPRNG unavailable");
    }
    return bytes;
}

std::string b64url_encode(const std::vector<std::uint8_t>& bytes) {
    static const char tbl[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    std::string out;
    size_t i = 0;
    for (; i + 3 <= bytes.size(); i += 3) {
        std::uint32_t v = (std::uint32_t(bytes[i]) << 16) |
                          (std::uint32_t(bytes[i + 1]) << 8) |
                          std::uint32_t(bytes[i + 2]);
        out.push_back(tbl[(v >> 18) & 0x3f]);
        out.push_back(tbl[(v >> 12) & 0x3f]);
        out.push_back(tbl[(v >> 6) & 0x3f]);
        out.push_back(tbl[v & 0x3f]);
    }
    if (i < bytes.size()) {
        std::uint32_t v = std::uint32_t(bytes[i]) << 16;
        if (i + 1 < bytes.size()) v |= std::uint32_t(bytes[i + 1]) << 8;
        out.push_back(tbl[(v >> 18) & 0x3f]);
        out.push_back(tbl[(v >> 12) & 0x3f]);
        if (i + 1 < bytes.size()) out.push_back(tbl[(v >> 6) & 0x3f]);
    }
    return out;  // unpadded
}

std::vector<std::uint8_t> b64url_decode(const std::string& s) {
    auto val = [](char c) -> int {
        if (c >= 'A' && c <= 'Z') return c - 'A';
        if (c >= 'a' && c <= 'z') return c - 'a' + 26;
        if (c >= '0' && c <= '9') return c - '0' + 52;
        if (c == '-') return 62;
        if (c == '_') return 63;
        return -1;
    };
    std::vector<std::uint8_t> out;
    int buf = 0;
    int bits = 0;
    for (char c : s) {
        if (c == '=') break;
        int v = val(c);
        if (v < 0) throw std::runtime_error("invalid base64url character");
        buf = (buf << 6) | v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out.push_back(static_cast<std::uint8_t>((buf >> bits) & 0xff));
        }
    }
    return out;
}

bool constant_time_eq(const std::string& a, const std::string& b) {
    if (a.size() != b.size()) {
        // Still touch a fixed amount of memory to reduce timing signal.
        volatile unsigned char sink = 0;
        for (char c : b) sink ^= static_cast<unsigned char>(c);
        (void)sink;
        return false;
    }
    unsigned int diff = 0;
    for (size_t i = 0; i < a.size(); ++i) {
        diff |= static_cast<unsigned int>(
            static_cast<unsigned char>(a[i]) ^
            static_cast<unsigned char>(b[i]));
    }
    return diff == 0;
}

}  // namespace pf::crypto
