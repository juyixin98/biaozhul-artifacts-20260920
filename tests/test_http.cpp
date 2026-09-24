// End-to-end HTTP test: a real gridfusion server on an ephemeral port
// driven by the real cpp-httplib client. Exercises the full JSON protocol:
// creation + version reuse, scan ingestion (wall/no-return/boundary/
// repeat), reference verify, unknown-vs-0.5 export, version isolation and
// every documented error case.

#include <chrono>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <thread>

// NOTE: include the gridfusion headers (which pull in Eigen) BEFORE
// httplib.h/json.hpp: some system headers transitively pulled in by the
// HTTP library define macros that break Eigen template parsing if Eigen is
// seen afterwards.
#include "gridfusion/server.hpp"

#include <httplib.h>
#include <nlohmann/json.hpp>

using json = nlohmann::json;
using gridfusion::MapRegistry;

static int failures = 0;
#define CHECK_GF(cond)                                                       \
    do {                                                                  \
        if (!(cond)) {                                                    \
            std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);  \
            ++failures;                                                   \
        }                                                                 \
    } while (0)

// get<double>() throws when the stored JSON number kind differs (int /
// unsigned / float); accept every numeric kind explicitly.
static double num(const json& j) {
    if (j.is_number_float()) return j.get<double>();
    if (j.is_number_unsigned())
        return static_cast<double>(j.get<unsigned long long>());
    if (j.is_number_integer())
        return static_cast<double>(j.get<long long>());
    throw std::runtime_error("JSON value is not a number");
}

int main() {
    httplib::Server srv;
    srv.set_payload_max_length(256ull * 1024 * 1024);
    MapRegistry registry;
    gridfusion::registerRoutes(srv, registry);

    // Bind an ephemeral port manually: listen on 0, read back the port.
    std::thread t;
    int port = 0;
    for (int candidate : {19080, 19081, 19090, 19100, 19200, 19300}) {
        port = candidate;
        t = std::thread([&, candidate] { srv.listen("127.0.0.1", candidate); });
        for (int i = 0; i < 100 && !srv.is_running(); ++i)
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        if (srv.is_running()) break;
        t.join();
    }
    if (!srv.is_running()) {
        std::printf("FAIL could not start test server\n");
        return 1;
    }

    httplib::Client cli("127.0.0.1", port);
    cli.set_connection_timeout(5);

    auto jpost = [&](const std::string& path, const json& body) {
        return cli.Post(path.c_str(), body.dump(), "application/json");
    };

    // ---- health ----
    auto h = cli.Get("/health");
    CHECK_GF(h && h->status == 200);
    CHECK_GF(json::parse(h->body)["status"] == "ok");

    // ---- create map ----
    json mapSpec = {{"resolution", 0.1},
                    {"origin_x", -3.0},
                    {"origin_y", -3.0},
                    {"width", 61},
                    {"height", 61},
                    {"default_max_range", 20.0}};
    auto r1 = jpost("/api/maps", mapSpec);
    CHECK_GF(r1 && r1->status == 201);
    const std::string version = json::parse(r1->body)["version"];
    CHECK_GF(version.size() == 64);

    // Same geometry -> reuse (200), same version id.
    auto r2 = jpost("/api/maps", mapSpec);
    CHECK_GF(r2 && r2->status == 200);
    CHECK_GF(json::parse(r2->body)["version"] == version);

    // Same geometry hash but different sensor params -> 409 conflict.
    json conflicting = mapSpec;
    conflicting["l_occ"] = 1.5;
    auto r3 = jpost("/api/maps", conflicting);
    CHECK_GF(r3 && r3->status == 409);

    // Changed resolution -> a distinct version (params never mixed).
    json otherRes = mapSpec;
    otherRes["resolution"] = 0.2;
    auto r4 = jpost("/api/maps", otherRes);
    CHECK_GF(r4 && r4->status == 201);
    const std::string v2 = json::parse(r4->body)["version"];
    CHECK_GF(v2 != version);

    // Invalid spec -> 400.
    auto rb = jpost("/api/maps", json{{"resolution", -1},
                                      {"width", 10},
                                      {"height", 10}});
    CHECK_GF(rb && rb->status == 400);

    const std::string scansPath = "/api/maps/" + version + "/scans";
    const std::string gridPath = "/api/maps/" + version + "/grid";

    // ---- wall scan: pose (0,0), wall ~3 m to the east ----
    json scan;
    {
        json beams = json::array();
        for (int k = -5; k <= 5; ++k) {
            const double ang = k * 0.05;
            beams.push_back(json{{"angle", ang},
                                 {"range", 3.0 / std::cos(ang)}});
        }
        // Two no-return beams (point north/south): frees only.
        beams.push_back(json{{"angle", M_PI / 2},
                             {"range", 0},
                             {"no_return", true}});
        beams.push_back(json{{"angle", -M_PI / 2},
                             {"range", 0},
                             {"no_return", true}});
        // A beam that leaves the map: range is within the sensor's
        // max_range but its endpoint lies beyond the map boundary.
        beams.push_back(json{{"angle", 0.3}, {"range", 4.0}});
        scan = json{{"pose", {{"x", 0}, {"y", 0}, {"theta", 0}}},
                    {"max_range", 20},
                    {"beams", beams}};
    }
    auto rs = jpost(scansPath, scan);
    CHECK_GF(rs && rs->status == 200);
    {
        auto st = json::parse(rs->body);
        CHECK_GF(st["version"] == version);
        CHECK_GF(st["hits"] == 12);  // 11 wall beams + 1 out-of-map return
        CHECK_GF(st["no_returns"] == 2);
        CHECK_GF(st["beams_clipped"] == 3);  // 2 no-return + 1 outside endpoint
        // The clipped return contributes no occupied update.
        CHECK_GF(st["occupied_updates"] == 11);
    }

    // ---- repeat the scan several times -> saturation on the wall ----
    for (int i = 0; i < 20; ++i) {
        auto rr = jpost(scansPath, scan);
        CHECK_GF(rr && rr->status == 200);
    }
    auto g = cli.Get(gridPath.c_str());
    CHECK_GF(g && g->status == 200);
    {
        auto gj = json::parse(g->body);
        CHECK_GF(gj["version"] == version);
        CHECK_GF(gj["width"] == 61 && gj["height"] == 61);
        CHECK_GF(gj["value"] == "probability");
        // Cell at world (3.0,0): index x=60,y=30 (origin -3, res 0.1).
        const auto& rows = gj["rows"];
        double pWall = num(rows[30][60]);
        CHECK_GF(pWall > 0.96);  // saturated near l_max (0.9707)
        CHECK_GF(pWall <= 1.0);
        // Unknown cell far away is null (distinct from observed 0.5).
        CHECK_GF(rows[5][5].is_null());
    }

    // Observed-0.5 distinctness over the wire: fresh map, cancelling scans.
    {
        json spec = {{"resolution", 1.0},
                     {"origin_x", 0.0},
                     {"origin_y", 0.0},
                     {"width", 6},
                     {"height", 3},
                     {"l_occ", 0.5},
                     {"l_free", -0.5}};
        auto cm = jpost("/api/maps", spec);
        CHECK_GF(cm && cm->status == 201);
        const std::string v = json::parse(cm->body)["version"];
        json sA = {{"pose", {{"x", 0.5}, {"y", 0.5}, {"theta", 0}}},
                   {"beams", json::array({{{"angle", 0}, {"range", 2}}})}};
        json sB = {{"pose", {{"x", 2.5}, {"y", 0.5}, {"theta", M_PI}}},
                   {"beams", json::array({{{"angle", 0}, {"range", 2}}})}};
        CHECK_GF(jpost("/api/maps/" + v + "/scans", sA)->status == 200);
        CHECK_GF(jpost("/api/maps/" + v + "/scans", sB)->status == 200);
        auto gg = cli.Get(("/api/maps/" + v + "/grid?bbox=1").c_str());
        CHECK_GF(gg);
        auto gj = json::parse(gg->body);
        // Middle cell (1,0) got free+free; endpoint cells cancel to 0.5 and
        // are observed -> the JSON value must be 0.5, not null.
        double pMid = num(gj["rows"][0][1]);
        CHECK_GF(pMid < 0.4);
        // sA endpoint (2,0), sB endpoint (0,0): observed exactly 0.5.
        double pE0 = num(gj["rows"][0][0]);
        double pE2 = num(gj["rows"][0][2]);
        CHECK_GF(std::fabs(pE0 - 0.5) < 1e-9);
        CHECK_GF(std::fabs(pE2 - 0.5) < 1e-9);
    }

    // ---- reference verify over HTTP must match exactly ----
    {
        json arr = json::array({scan});
        auto vfy = jpost("/api/maps/" + version + "/verify", arr);
        CHECK_GF(vfy && vfy->status == 200);
        auto vj = json::parse(vfy->body);
        CHECK_GF(vj["matches_reference"] == true);
        CHECK_GF(vj["differing_cells"] == 0);
        CHECK_GF(vj["max_abs_logit_diff"] == 0.0);
    }

    // ---- log-odds + CSV export ----
    {
        auto lo = cli.Get((gridPath + "?logodds=1&bbox=1").c_str());
        CHECK_GF(lo && lo->status == 200);
        auto lj = json::parse(lo->body);
        CHECK_GF(lj["value"] == "logodds");
        auto csv = cli.Get(
            ("/api/maps/" + version + "/grid.csv?bbox=1").c_str());
        CHECK_GF(csv && csv->status == 200);
        CHECK_GF(csv->body.find("# version=") == 0);
        CHECK_GF(csv->body.find('?') != std::string::npos);  // unknown marker
    }

    // ---- error cases ----
    CHECK_GF(cli.Get("/api/maps/deadbeef/grid")->status == 404);
    CHECK_GF(cli.Post(scansPath.c_str(), "not json", "application/json")
              ->status == 400);
    {
        json bad = {{"pose", {{"x", 1000}, {"y", 0}, {"theta", 0}}},
                    {"beams", json::array()}};
        CHECK_GF(jpost(scansPath, bad)->status == 422);  // sensor outside map
        json bad2 = {{"pose", {{"x", 0}, {"y", 0}, {"theta", 0}}},
                     {"beams", json::array(
                         {{{"angle", 0}, {"range", 99}}})}};  // > max_range
        CHECK_GF(jpost(scansPath, bad2)->status == 422);
        json bad3 = {{"pose", {{"x", 0}, {"y", 0}, {"theta", 0}}}};
        CHECK_GF(jpost(scansPath, bad3)->status == 400);  // no beams
    }

    // ---- version isolation: scans for v2 must not touch version ----
    {
        json s = {{"pose", {{"x", 0}, {"y", 0}, {"theta", 0}}},
                  {"max_range", 5},
                  {"beams", json::array({{{"angle", 0}, {"range", 1}}})}};
        CHECK_GF(jpost("/api/maps/" + v2 + "/scans", s)->status == 200);
        auto lm = cli.Get("/api/maps");
        CHECK_GF(lm && lm->status == 200);
        CHECK_GF(json::parse(lm->body)["maps"].size() >= 2);
    }

    srv.stop();
    t.join();

    if (failures) {
        std::printf("%d HTTP check(s) failed\n", failures);
        return 1;
    }
    std::printf("all HTTP tests passed\n");
    return 0;
}
