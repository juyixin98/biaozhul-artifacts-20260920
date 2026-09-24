// JSON protocol (de)serialization and request-signing canonicalization.
#pragma once

#include "fusion.hpp"
#include "service.hpp"
#include "types.hpp"

#include <nlohmann/json.hpp>

#include <cstdint>
#include <string>

namespace pf {

using Json = nlohmann::json;

struct ProtocolError {
    int status = 400;
    std::string code;
    std::string message;
};

struct ParsedScan {
    ScanInput scan;
    bool has_resolution = false;
    double resolution = 0.0;
    bool has_origin = false;
    double origin_x = 0.0;
    double origin_y = 0.0;
    bool has_base_seq = false;
    std::uint64_t base_seq = 0;
};

Json configJson(const MapConfig& cfg);
Json paramsJson(const FusionParams& p);
Json versionJson(const GridVersion& v);   // metadata only (no grid payload)
Json mapSummaryJson(const MapRecord& rec);
Json gridExportJson(const MapRecord& rec, const GridVersion& v);
Json versionDetailJson(const MapRecord& rec, const GridVersion& v);

CreateMapOptions parseCreateMap(const Json& body);
ParsedScan parseScan(const Json& body);

// Canonical request string authenticated with HMAC-SHA256:
//   SIGNED-REQUEST:v1\n
//   method=<upper>\n
//   target=<path?query-with-sorted-query-pairs>\n
//   timestamp=<unix-seconds>\n
//   nonce=<string>\n
//   body_sha256=<hex; sha256 of empty string for an empty body>\n
std::string canonicalRequest(const std::string& method,
                             const std::string& target,
                             std::int64_t timestamp,
                             const std::string& nonce,
                             const std::string& body_sha256_hex);

// Sort query pairs of a request target ("path?b=2&a=1" ->
// "path?a=1&b=2"). Preserves the path verbatim.
std::string canonicalizeTarget(const std::string& target);

Json errorBody(const std::string& code, const std::string& message);

}  // namespace pf
