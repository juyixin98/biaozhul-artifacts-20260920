#include "http_server.hpp"

#include "crypto.hpp"
#include "protocol.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <ctime>
#include <thread>
#include <utility>
#include <vector>

namespace pf {

namespace {

std::int64_t nowUnix() {
    return static_cast<std::int64_t>(std::time(nullptr));
}

void sendErr(httplib::Response& res, int status, const std::string& code,
             const std::string& msg) {
    res.status = status;
    res.set_content(errorBody(code, msg).dump(), "application/json");
}

// Parse JSON body; empty body parses as an object for POST routes that may
// rely purely on query parameters.
Json parseJson(const httplib::Request& req, bool empty_as_object) {
    try {
        if (req.body.empty())
            return empty_as_object ? Json::object() : Json();
        return Json::parse(req.body);
    } catch (const std::exception&) {
        throw ProtocolError{400, "INVALID_JSON", "request body is not valid JSON"};
    }
}

}  // namespace

FusionHttpServer::FusionHttpServer(MapService& svc, ServerOptions opt)
    : svc_(svc), opt_(std::move(opt)) {}

bool FusionHttpServer::authenticate(const httplib::Request& req,
                                    std::string& error_code) {
    auto it_ts = req.headers.find("X-PF-Timestamp");
    auto it_n = req.headers.find("X-PF-Nonce");
    auto it_s = req.headers.find("X-PF-Signature");
    if (it_ts == req.headers.end() || it_n == req.headers.end() ||
        it_s == req.headers.end()) {
        error_code = "MISSING_CREDENTIALS";
        return false;
    }
    std::int64_t ts = 0;
    try {
        ts = std::stoll(it_ts->second);
    } catch (...) {
        error_code = "BAD_TIMESTAMP";
        return false;
    }
    std::int64_t now = nowUnix();
    if (std::llabs(static_cast<long long>(ts - now)) >
        opt_.timestamp_tolerance_seconds) {
        error_code = "TIMESTAMP_SKEW";
        return false;
    }
    const std::string& nonce = it_n->second;
    if (nonce.empty() || nonce.size() > 256) {
        error_code = "BAD_NONCE";
        return false;
    }
    {
        std::lock_guard<std::mutex> lock(nonce_mu_);
        std::string key = std::to_string(ts) + ":" + nonce;
        if (seen_nonces_.count(key)) {
            error_code = "REPLAY_DETECTED";
            return false;
        }
        seen_nonces_.insert(std::move(key));
        if (seen_nonces_.size() > 4096) {
            std::int64_t cutoff = now - 2 * opt_.timestamp_tolerance_seconds;
            std::vector<std::string> keep;
            for (const auto& k : seen_nonces_) {
                auto colon = k.find(':');
                if (colon == std::string::npos) continue;
                try {
                    if (std::stoll(k.substr(0, colon)) >= cutoff)
                        keep.push_back(k);
                } catch (...) {
                    // drop malformed
                }
            }
            seen_nonces_.clear();
            seen_nonces_.insert(keep.begin(), keep.end());
        }
    }

    std::string body_hash = crypto::sha256_hex(req.body);
    std::string canonical =
        canonicalRequest(req.method, req.target, ts, nonce, body_hash);
    std::string expected = crypto::hmac_sha256_hex(opt_.secret, canonical);
    if (!crypto::constant_time_eq(expected, it_s->second)) {
        error_code = "BAD_SIGNATURE";
        return false;
    }
    return true;
}

int FusionHttpServer::run() {
    auto srv = std::make_shared<httplib::Server>();
    {
        std::lock_guard<std::mutex> lock(http_mu_);
        http_ = srv;
    }
    srv->set_payload_max_length(opt_.max_body_bytes);
    srv->set_keep_alive_max_count(16);

    auto require_auth = [this](const httplib::Request& req,
                               httplib::Response& res) -> bool {
        std::string code;
        if (!authenticate(req, code)) {
            sendErr(res, 401, code, "request authentication failed");
            return false;
        }
        return true;
    };

    srv->Get("/health", [&](const httplib::Request&, httplib::Response& res) {
        Json j{{"status", "ok"},
               {"service", "grid-probability-fusion"},
               {"time_unix", nowUnix()}};
        res.set_content(j.dump(), "application/json");
    });

    // ---- create map ----
    srv->Post("/v1/maps", [&](const httplib::Request& req,
                             httplib::Response& res) {
        if (!require_auth(req, res)) return;
        Json body;
        try {
            body = parseJson(req, true);
        } catch (const ProtocolError& e) {
            return sendErr(res, e.status, e.code, e.message);
        }
        try {
            CreateMapOptions opt = parseCreateMap(body);
            MapRecord& rec = svc_.createMap(opt, nowUnix());
            std::lock_guard<std::mutex> lock(rec.mu);
            Json out = mapSummaryJson(rec);
            out["version"] = versionJson(rec.versions.back());
            out["config_digest"] = crypto::sha256_hex(
                OccupancyGrid::configCanonical(rec.config, rec.params));
            res.status = 201;
            res.set_content(out.dump(), "application/json");
        } catch (const ProtocolError& e) {
            sendErr(res, e.status, e.code, e.message);
        } catch (const std::exception& e) {
            sendErr(res, 400, "INVALID_REQUEST", e.what());
        }
    });

    // ---- list maps ----
    srv->Get("/v1/maps", [&](const httplib::Request& req,
                            httplib::Response& res) {
        if (!require_auth(req, res)) return;
        Json arr = Json::array();
        for (const std::string& id : svc_.listIds()) {
            MapRecord* rec = svc_.find(id);
            if (!rec) continue;
            std::lock_guard<std::mutex> lock(rec->mu);
            arr.push_back(mapSummaryJson(*rec));
        }
        res.set_content(Json{{"maps", arr}}.dump(), "application/json");
    });

    // ---- map metadata ----
    srv->Get(R"(/v1/maps/([A-Za-z0-9_\-]+))",
            [&](const httplib::Request& req, httplib::Response& res) {
        if (!require_auth(req, res)) return;
        MapRecord* rec = svc_.find(req.matches[1]);
        if (!rec) return sendErr(res, 404, "NOT_FOUND", "unknown map id");
        std::lock_guard<std::mutex> lock(rec->mu);
        Json out = mapSummaryJson(*rec);
        Json vers = Json::array();
        for (const auto& v : rec->versions) vers.push_back(versionJson(v));
        out["versions"] = vers;
        res.set_content(out.dump(), "application/json");
    });

    // ---- apply scan ----
    srv->Post(R"(/v1/maps/([A-Za-z0-9_\-]+)/scans)",
             [&](const httplib::Request& req, httplib::Response& res) {
        if (!require_auth(req, res)) return;
        MapRecord* rec = svc_.find(req.matches[1]);
        if (!rec) return sendErr(res, 404, "NOT_FOUND", "unknown map id");

        Json body;
        try {
            body = parseJson(req, true);
        } catch (const ProtocolError& e) {
            return sendErr(res, e.status, e.code, e.message);
        }
        ParsedScan ps;
        try {
            ps = parseScan(body);
        } catch (const ProtocolError& e) {
            return sendErr(res, e.status, e.code, e.message);
        }

        std::lock_guard<std::mutex> lock(rec->mu);
        // Resolution + origin are version-bound: refuse to mix an
        // incompatible parameter declaration into the existing grid.
        if (ps.has_resolution && ps.resolution != rec->config.resolution) {
            return sendErr(res, 409, "PARAM_MISMATCH",
                           "scan resolution differs from map version; create a "
                           "new map instead of mixing cells into an old grid");
        }
        if (ps.has_origin &&
            (ps.origin_x != rec->config.origin_x ||
             ps.origin_y != rec->config.origin_y)) {
            return sendErr(res, 409, "PARAM_MISMATCH",
                           "scan origin differs from map version; create a new "
                           "map instead of mixing cells into an old grid");
        }
        try {
            GridVersion v = applyScanVersioned(
                *rec, ps.scan, nowUnix(), ps.base_seq, ps.has_base_seq);
            Json out = mapSummaryJson(*rec);
            out["version"] = versionJson(v);
            res.set_content(out.dump(), "application/json");
        } catch (const std::exception& e) {
            sendErr(res, 409, "VERSION_CONFLICT", e.what());
        }
    });

    // ---- export grid (optionally at an older seq) ----
    srv->Get(R"(/v1/maps/([A-Za-z0-9_\-]+)/grid)",
            [&](const httplib::Request& req, httplib::Response& res) {
        if (!require_auth(req, res)) return;
        MapRecord* rec = svc_.find(req.matches[1]);
        if (!rec) return sendErr(res, 404, "NOT_FOUND", "unknown map id");
        std::uint64_t seq = UINT64_MAX;  // latest unless ?seq= supplied
        if (req.has_param("seq")) {
            try {
                seq = std::stoull(req.get_param_value("seq"));
            } catch (...) {
                return sendErr(res, 400, "INVALID_REQUEST", "bad seq");
            }
        }
        std::lock_guard<std::mutex> lock(rec->mu);
        if (seq == UINT64_MAX) seq = rec->versions.back().seq;
        if (seq >= rec->versions.size())
            return sendErr(res, 404, "NOT_FOUND", "unknown version seq");
        res.set_content(
            gridExportJson(*rec, rec->versions[static_cast<size_t>(seq)])
                .dump(),
            "application/json");
    });

    // ---- versions ----
    srv->Get(R"(/v1/maps/([A-Za-z0-9_\-]+)/versions)",
            [&](const httplib::Request& req, httplib::Response& res) {
        if (!require_auth(req, res)) return;
        MapRecord* rec = svc_.find(req.matches[1]);
        if (!rec) return sendErr(res, 404, "NOT_FOUND", "unknown map id");
        std::lock_guard<std::mutex> lock(rec->mu);
        Json arr = Json::array();
        for (const auto& v : rec->versions)
            arr.push_back(versionDetailJson(*rec, v));
        res.set_content(Json{{"map_id", rec->id}, {"versions", arr}}.dump(),
                        "application/json");
    });

    // ---- integrity verification ----
    srv->Get(R"(/v1/maps/([A-Za-z0-9_\-]+)/verify)",
            [&](const httplib::Request& req, httplib::Response& res) {
        if (!require_auth(req, res)) return;
        MapRecord* rec = svc_.find(req.matches[1]);
        if (!rec) return sendErr(res, 404, "NOT_FOUND", "unknown map id");
        std::lock_guard<std::mutex> lock(rec->mu);
        std::string parent(64, '0');
        bool ok = true;
        std::string first_bad;
        for (const auto& v : rec->versions) {
            std::string expect_state =
                OccupancyGrid::stateDigestFrom(rec->config, v.snapshot);
            std::string expect_ver =
                chainDigest(v.seq, parent, v.payload_digest, expect_state);
            if (expect_state != v.state_digest ||
                expect_ver != v.version_digest) {
                ok = false;
                first_bad = std::to_string(v.seq);
                break;
            }
            parent = v.version_digest;
        }
        Json out{
            {"ok", ok},
            {"map_id", rec->id},
            {"latest_seq", rec->versions.back().seq},
            {"latest_version_digest", rec->versions.back().version_digest}};
        if (!ok) out["first_bad_seq"] = first_bad;
        res.set_content(out.dump(), "application/json");
    });

    srv->set_exception_handler(
        [&](const httplib::Request&, httplib::Response& res,
            std::exception_ptr ep) {
            try {
                if (ep) std::rethrow_exception(ep);
            } catch (const std::exception& e) {
                sendErr(res, 500, "INTERNAL", e.what());
            }
        });

    // Bind first (allow port retries / ephemeral port), then serve.
    const int base_port = opt_.port;
    int bound = 0;
    if (base_port == 0) {
        bound = srv->bind_to_any_port(opt_.host);  // <0 on failure
    } else {
        for (int a = 0; a < 32; ++a) {
            if (srv->bind_to_port(opt_.host, base_port + a)) {
                bound = base_port + a;
                break;
            }
        }
    }
    if (bound <= 0) {
        std::lock_guard<std::mutex> lock(http_mu_);
        http_.reset();
        return 0;
    }
    bound_port_ = bound;
    srv->listen_after_bind();  // blocks until stop()
    {
        std::lock_guard<std::mutex> lock(http_mu_);
        http_.reset();
    }
    return bound;
}

void FusionHttpServer::stop() {
    stopping_ = true;
    std::shared_ptr<httplib::Server> srv;
    {
        std::lock_guard<std::mutex> lock(http_mu_);
        srv = http_;
    }
    if (srv) srv->stop();  // closes the listening socket, unblocking run()
}

// ---------------------------------------------------------------------------
// ServerRunner
// ---------------------------------------------------------------------------
ServerRunner::ServerRunner(MapService& svc, ServerOptions opt)
    : server_(svc, std::move(opt)) {}

bool ServerRunner::waitReady(int timeout_ms) {
    port_ = server_.opt_.port;
    thread_ = std::thread([this] { port_ = server_.run(); });

    auto deadline = std::chrono::steady_clock::now() +
                    std::chrono::milliseconds(timeout_ms);
    while (std::chrono::steady_clock::now() < deadline) {
        if (server_.boundPort() != 0) {
            httplib::Client c("127.0.0.1", server_.boundPort());
            c.set_connection_timeout(0, 200000);  // 200 ms
            c.set_read_timeout(0, 200000);
            auto r = c.Get("/health");
            if (r && r->status == 200) {
                port_ = server_.boundPort();
                return true;
            }
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return false;
}

void ServerRunner::stop() {
    server_.stop();  // closes listening socket; run() thread then returns
    if (thread_.joinable()) thread_.join();
}

ServerRunner::~ServerRunner() {
    stop();
}

}  // namespace pf
