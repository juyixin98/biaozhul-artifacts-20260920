#include "fusion.hpp"

#include "format.hpp"

#include <cmath>
#include <iomanip>
#include <sstream>
#include <stdexcept>

namespace pf {

std::string canonicalScan(const ScanInput& scan) {
    std::ostringstream os;
    os << "SCAN:v1\n";
    os << "pose=" << formatDouble(scan.pose.x) << ','
       << formatDouble(scan.pose.y) << ','
       << formatDouble(scan.pose.theta) << '\n';
    os << "beams=" << scan.beams.size() << '\n';
    for (const Beam& b : scan.beams) {
        os << (b.hit ? "H" : "R") << ' ' << formatDouble(b.ox) << ' '
           << formatDouble(b.oy) << ' ' << formatDouble(b.ex) << ' '
           << formatDouble(b.ey);
        if (!b.hit) os << ' ' << formatDouble(b.max_range);
        os << '\n';
    }
    return os.str();
}

std::string chainDigest(std::uint64_t seq, const std::string& parent,
                        const std::string& payload,
                        const std::string& state) {
    std::ostringstream os;
    os << "GRID-VERSION:v1\n";
    os << "seq=" << seq << '\n';
    os << "parent=" << parent << '\n';
    os << "payload=" << payload << '\n';
    os << "state=" << state << '\n';
    return crypto::sha256_hex(os.str());
}

void initializeEmptyVersion(MapRecord& rec) {
    GridVersion v0;
    v0.seq = 0;
    v0.payload_digest = crypto::sha256_hex("");
    GridSnapshot empty;
    const int n = rec.config.width * rec.config.height;
    empty.log_odds.assign(n, 0.0);
    empty.observed.assign(n, 0);
    empty.free_touches.assign(n, 0);
    empty.hit_touches.assign(n, 0);
    v0.snapshot = std::move(empty);
    v0.state_digest =
        OccupancyGrid::stateDigestFrom(rec.config, v0.snapshot);
    v0.version_digest =
        chainDigest(0, std::string(64, '0'), v0.payload_digest, v0.state_digest);
    v0.created_unix = rec.created_unix;
    rec.versions.push_back(std::move(v0));
}

GridVersion applyScanVersioned(MapRecord& rec, const ScanInput& scan,
                               std::int64_t now_unix,
                               std::uint64_t expected_seq,
                               bool check_expected) {
    std::uint64_t cur_seq = rec.versions.size() - 1;
    if (check_expected && expected_seq != cur_seq) {
        std::ostringstream os;
        os << "expected base seq " << cur_seq << " but client asserted "
           << expected_seq;
        throw std::invalid_argument(os.str());
    }

    // Geometry validation: reject NaN/inf so no corrupt coordinates reach the
    // ray caster; a no-return beam must carry a positive max_range.
    auto good = [](double v) { return std::isfinite(v); };
    if (!good(scan.pose.x) || !good(scan.pose.y) || !good(scan.pose.theta))
        throw std::invalid_argument("pose contains non-finite value");
    for (const Beam& b : scan.beams) {
        if (!good(b.ox) || !good(b.oy) || !good(b.ex) || !good(b.ey))
            throw std::invalid_argument("beam contains non-finite value");
        if (!b.hit && (!(b.max_range > 0.0) || !good(b.max_range)))
            throw std::invalid_argument(
                "no-return beam requires finite positive max_range");
    }

    UpdateCounts counts = rec.grid->applyScan(scan);

    GridVersion v;
    v.seq = cur_seq + 1;
    v.created_unix = now_unix;
    v.counts = counts;
    std::string payload = canonicalScan(scan);
    v.payload_digest = crypto::sha256_hex(payload);
    v.snapshot = rec.grid->snapshot();
    v.state_digest =
        OccupancyGrid::stateDigestFrom(rec.config, v.snapshot);
    v.version_digest =
        chainDigest(v.seq, rec.versions.back().version_digest,
                    v.payload_digest, v.state_digest);
    rec.versions.push_back(std::move(v));
    return rec.versions.back();
}

}  // namespace pf
