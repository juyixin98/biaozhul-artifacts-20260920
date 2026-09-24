#include "gridfusion/server.hpp"

#include <iostream>
#include <utility>

#include <httplib.h>
#include <nlohmann/json.hpp>

#include "gridfusion/protocol.hpp"

namespace gridfusion {

MapRegistry::CreateResult MapRegistry::getOrCreate(const MapConfig& cfg) {
    const std::string id = GridMap::make_version_id(cfg);
    std::lock_guard<std::mutex> lock(mtx_);
    auto it = entries_.find(id);
    if (it != entries_.end()) {
        CreateResult cr;
        cr.entry = it->second.get();
        if (!(it->second->config == cfg)) {
            cr.kind = CreateResult::Kind::Conflict;
            cr.detail =
                "map geometry already exists under this version with "
                "different sensor parameters";
        } else {
            cr.kind = CreateResult::Kind::Reused;
        }
        return cr;
    }
    auto entry = std::make_shared<Entry>();
    entry->version = id;
    entry->config = cfg;
    entry->map = std::make_unique<GridMap>(cfg);
    Entry* ptr = entry.get();
    entries_.emplace(id, std::move(entry));
    return {ptr, CreateResult::Kind::Created, ""};
}

MapRegistry::Entry* MapRegistry::find(const std::string& version) {
    std::lock_guard<std::mutex> lock(mtx_);
    auto it = entries_.find(version);
    return it == entries_.end() ? nullptr : it->second.get();
}

std::vector<std::pair<std::string, MapConfig>> MapRegistry::list() const {
    std::lock_guard<std::mutex> lock(mtx_);
    std::vector<std::pair<std::string, MapConfig>> out;
    out.reserve(entries_.size());
    for (const auto& [id, e] : entries_)
        out.emplace_back(id, e->config);
    return out;
}

namespace {

using nlohmann::json;

void sendError(httplib::Response& res, int status, const std::string& msg) {
    res.status = status;
    res.set_content(json{{"error", msg}}.dump(), "application/json");
}

// NOTE: this must be a file-scope function, not a local lambda: the route
// handlers outlive registerRoutes(), and capturing a stack-local lambda by
// reference would dangle the first time a request arrived after return.
MapRegistry::Entry* findEntry(MapRegistry& registry,
                              const std::string& version,
                              httplib::Response& res) {
    MapRegistry::Entry* e = registry.find(version);
    if (!e) sendError(res, 404, "unknown map version: " + version);
    return e;
}

void handleExport(MapRegistry& registry, const httplib::Request& req,
                  httplib::Response& res, bool csv) {
    MapRegistry::Entry* e =
        findEntry(registry, std::string(req.matches[1]), res);
    if (!e) return;
    proto::ExportOptions opt;
    if (req.has_param("logodds"))
        opt.logodds = req.get_param_value("logodds") != "0";
    if (req.has_param("bbox"))
        opt.bbox_only = req.get_param_value("bbox") != "0";
    std::lock_guard<std::mutex> lock(e->mtx);
    if (csv) {
        res.set_content(proto::gridToCsv(*e->map, opt), "text/csv");
    } else {
        res.set_content(proto::gridToJson(*e->map, opt),
                        "application/json");
    }
}

}  // namespace

// Builds and registers every route on an httplib::Server instance. Exposed
// separately so the integration test can bind an ephemeral port.
void registerRoutes(httplib::Server& srv, MapRegistry& registry) {
    srv.Get("/health",
            [&](const httplib::Request&, httplib::Response& res) {
                res.set_content(json{{"status", "ok"}}.dump(),
                                "application/json");
            });

    srv.Get("/api/maps",
            [&](const httplib::Request&, httplib::Response& res) {
                res.set_content(proto::mapListToJson(registry.list()),
                                "application/json");
            });

    srv.Post("/api/maps",
             [&](const httplib::Request& req, httplib::Response& res) {
                 proto::ParseResult pr;
                 MapConfig cfg = proto::parseConfig(req.body, pr);
                 if (!pr.ok) return sendError(res, pr.status, pr.error);
                 MapRegistry::CreateResult cr = registry.getOrCreate(cfg);
                 if (cr.kind == MapRegistry::CreateResult::Kind::Conflict)
                     return sendError(res, 409, cr.detail);
                 res.status =
                     cr.kind == MapRegistry::CreateResult::Kind::Reused
                         ? 200
                         : 201;
                 res.set_content(
                     proto::configToJson(cfg, cr.entry->version),
                     "application/json");
             });

    srv.Post(R"(/api/maps/([0-9a-f]{64})/scans)",
             [&](const httplib::Request& req, httplib::Response& res) {
                 MapRegistry::Entry* e =
                     findEntry(registry, std::string(req.matches[1]), res);
                 if (!e) return;
                 proto::ParseResult pr;
                 Scan scan = proto::parseScan(req.body, pr);
                 if (!pr.ok) return sendError(res, pr.status, pr.error);
                 if (const std::string err =
                         proto::validateScanForMap(scan, e->config);
                     !err.empty())
                     return sendError(res, 422, err);
                 ScanStats stats;
                 {
                     std::lock_guard<std::mutex> lock(e->mtx);
                     stats = e->map->update(scan);
                 }
                 res.set_content(proto::statsToJson(stats, e->version),
                                 "application/json");
             });

    srv.Get(R"(/api/maps/([0-9a-f]{64})/grid)",
            [&](const httplib::Request& req, httplib::Response& res) {
                handleExport(registry, req, res, false);
            });
    srv.Get(R"(/api/maps/([0-9a-f]{64})/grid.csv)",
            [&](const httplib::Request& req, httplib::Response& res) {
                handleExport(registry, req, res, true);
            });

    // Replay the posted scans on throwaway copies with both traversals and
    // return the per-cell disagreement (acceptance requires zero).
    srv.Post(R"(/api/maps/([0-9a-f]{64})/verify)",
             [&](const httplib::Request& req, httplib::Response& res) {
                 MapRegistry::Entry* e =
                     findEntry(registry, std::string(req.matches[1]), res);
                 if (!e) return;
                 json body;
                 try {
                     body = json::parse(req.body);
                 } catch (const std::exception& ex) {
                     return sendError(res, 400,
                                      std::string("invalid JSON: ") +
                                          ex.what());
                 }
                 if (!body.is_array())
                     return sendError(
                         res, 400,
                         "body must be an array of scan JSON objects");
                 std::vector<Scan> scans;
                 scans.reserve(body.size());
                 for (const json& sj : body) {
                     proto::ParseResult pr;
                     Scan scan = proto::parseScan(sj.dump(), pr);
                     if (!pr.ok)
                         return sendError(res, pr.status, pr.error);
                     if (const std::string err =
                             proto::validateScanForMap(scan, e->config);
                         !err.empty())
                         return sendError(res, 422, err);
                     scans.push_back(std::move(scan));
                 }
                 GridMap::ReferenceDiff d =
                     GridMap::verifyAgainstReference(e->config, scans);
                 json out;
                 out["version"] = e->version;
                 out["cell_count"] = d.cell_count;
                 out["differing_cells"] = d.differing_cells;
                 out["max_abs_logit_diff"] = d.max_abs_logit_diff;
                 out["matches_reference"] = d.differing_cells == 0;
                 res.set_content(out.dump(2), "application/json");
             });

    srv.set_exception_handler(
        [](const httplib::Request&, httplib::Response& res,
           const std::exception_ptr& ep) {
            try {
                if (ep) std::rethrow_exception(ep);
            } catch (const std::exception& ex) {
                sendError(res, 500,
                          std::string("internal error: ") + ex.what());
            }
        });
}

int runServer(const ServerConfig& sc) {
    httplib::Server srv;
    srv.set_payload_max_length(256ull * 1024 * 1024);
    MapRegistry registry;
    registerRoutes(srv, registry);

    const int port = sc.port > 0 ? sc.port : 8080;
    std::cout << "gridfusion HTTP service listening on http://" << sc.host
              << ':' << port << '\n'
              << std::flush;
    if (!srv.listen(sc.host, port)) {
        std::cerr << "failed to bind " << sc.host << ':' << port << '\n';
        return 1;
    }
    return 0;
}

}  // namespace gridfusion
