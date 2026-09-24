#pragma once
// HTTP service: map registry (geometry-hash versioning) plus the scan
// ingestion and numeric-export routes. Backend only — no HTML/UI is served.

#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "gridfusion/grid_map.hpp"
#include "gridfusion/types.hpp"

namespace httplib {
class Server;
}

namespace gridfusion {

class MapRegistry {
public:
    struct Entry {
        std::string version;
        MapConfig config;
        std::unique_ptr<GridMap> map;
        mutable std::mutex mtx;
    };

    struct CreateResult {
        enum class Kind { Created, Reused, Conflict };
        Entry* entry = nullptr;
        Kind kind = Kind::Created;
        std::string detail;
    };

    // Finds by geometry hash; creates when absent. Reusing the same geometry
    // with a different sensor model is a conflict (params must not be mixed).
    CreateResult getOrCreate(const MapConfig& cfg);
    Entry* find(const std::string& version);
    std::vector<std::pair<std::string, MapConfig>> list() const;

private:
    mutable std::mutex mtx_;
    std::map<std::string, std::shared_ptr<Entry>> entries_;
};

// Runs the blocking HTTP loop (default host/port below).
struct ServerConfig {
    std::string host = "127.0.0.1";
    int port = 8080;
};

// Register all routes on a caller-owned server (used by the integration
// test to bind an ephemeral port without touching the network config).
void registerRoutes(httplib::Server& srv, MapRegistry& registry);

int runServer(const ServerConfig& sc);

}  // namespace gridfusion
