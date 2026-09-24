// Version-chain logic: a map binds a resolution + origin + fusion parameters
// for its whole lifetime. Every applied scan appends an immutable version
// whose digest chains HMAC-style over the prior version and the canonical
// content of the scan and resulting grid state.
#pragma once

#include "crypto.hpp"
#include "grid.hpp"

#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace pf {

// Immutable record of one applied scan.
struct GridVersion {
    std::uint64_t seq = 0;                 // 0 = initial empty version
    std::string payload_digest;            // SHA-256 of canonical scan ("" for v0)
    std::string state_digest;              // grid state after the update
    std::string version_digest;            // chain digest of this version
    std::int64_t created_unix = 0;
    GridSnapshot snapshot;                 // full snapshot for version export
    UpdateCounts counts{};
    std::string pose_digest;               // informational: canonical pose+beams hash == payload
};

struct MapRecord {
    std::string id;
    MapConfig config;
    FusionParams params;
    std::int64_t created_unix = 0;
    std::unique_ptr<OccupancyGrid> grid;
    std::vector<GridVersion> versions;     // versions[0] is the empty v0
    mutable std::mutex mu;
};

// Canonical textual form of one scan payload (floating point uses %.17g).
std::string canonicalScan(const ScanInput& scan);

// Compute the chained digest of a new version:
//   "GRID-VERSION:v1\nseq=..\nparent=<hex>\npayload=<hex>\nstate=<hex>\n"
// hashed with SHA-256.
std::string chainDigest(std::uint64_t seq, const std::string& parent,
                        const std::string& payload,
                        const std::string& state);

// Append v0 (empty) to a freshly constructed record. Caller does not need to
// hold the map lock for this initialization.
void initializeEmptyVersion(MapRecord& rec);

// Apply one scan and append a version; returns the new version by value.
// Throws std::invalid_argument on geometry failure.
GridVersion applyScanVersioned(MapRecord& rec, const ScanInput& scan,
                               std::int64_t now_unix,
                               std::uint64_t expected_seq,
                               bool check_expected);

}  // namespace pf
