#pragma once
// JSON <-> domain conversion, strict input validation and numeric-grid
// export formats (JSON rows + CSV). No graphics: the export is pure data.

#include <string>
#include <vector>

#include "gridfusion/grid_map.hpp"
#include "gridfusion/types.hpp"

namespace gridfusion::proto {

struct ParseResult {
    bool ok = false;
    int status = 400;
    std::string error;
};

MapConfig parseConfig(const std::string& body, ParseResult& r);
Scan parseScan(const std::string& body, ParseResult& r);

// Full scan validation against a map: pose inside, finite ranges,
// range <= max_range on returned beams, etc. Empty-error string = valid.
std::string validateScanForMap(const Scan& scan, const MapConfig& cfg);

std::string configToJson(const MapConfig& cfg, const std::string& version);
std::string statsToJson(const ScanStats& stats, const std::string& version);

struct ExportOptions {
    bool logodds = false;   // export log-odds instead of probability
    bool bbox_only = false; // crop to the observed bounding box
};

std::string gridToJson(const GridMap& map, const ExportOptions& opt);
std::string gridToCsv(const GridMap& map, const ExportOptions& opt);

std::string mapListToJson(
    const std::vector<std::pair<std::string, MapConfig>>& maps);

}  // namespace gridfusion::proto
