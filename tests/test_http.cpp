#include "crypto.hpp"
#include "http_server.hpp"
#include "protocol.hpp"
#include "service.hpp"

#include <httplib.h>

#include "test_util.hpp"

#include <ctime>
#include <string>

using namespace pf;

namespace {

const std::string kSecret = "unit-test-secret-please-do-not-use-elsewhere";

std::int64_t nowTs() {
    return static_cast<std::int64_t>(std::time(nullptr));
}

httplib::Headers authHeaders(const std::string& secret,
                             const std::string& method,
                             const std::string& target,
                             const std::string& body,
                             std::int64_t ts,
                             const std::string& nonce) {
    std::string canonical = canonicalRequest(
        method, target, ts, nonce, crypto::sha256_hex(body));
    return {
        {"X-PF-Timestamp", std::to_string(ts)},
        {"X-PF-Nonce", nonce},
        {"X-PF-Signature", crypto::hmac_sha256_hex(secret, canonical)},
    };
}

// GET with signed headers. target may contain a query string.
httplib::Result sget(httplib::Client& cli, const std::string& target,
                     const std::string& secret, const std::string& nonce,
                     std::int64_t ts = nowTs()) {
    return cli.Get(target.c_str(),
                   authHeaders(secret, "GET", target, "", ts, nonce));
}

httplib::Result spost(httplib::Client& cli, const std::string& target,
                      const std::string& body, const std::string& secret,
                      const std::string& nonce,
                      std::int64_t ts = nowTs()) {
    return cli.Post(target.c_str(),
                    authHeaders(secret, "POST", target, body, ts, nonce),
                    body, "application/json");
}

// Sign one body (signed_body) but transmit a different body (sent_body):
// a genuine tampered request that must fail signature verification.
httplib::Result spostTampered(httplib::Client& cli, const std::string& target,
                              const std::string& signed_body,
                              const std::string& sent_body,
                              const std::string& secret,
                              const std::string& nonce,
                              std::int64_t ts = nowTs()) {
    httplib::Request req;
    req.method = "POST";
    auto q = target.find('?');
    req.path = q == std::string::npos ? target : target.substr(0, q);
    req.target = target;
    req.headers = authHeaders(secret, "POST", target, signed_body, ts, nonce);
    req.body = sent_body;
    req.set_header("Content-Type", "application/json");
    return cli.send(req);
}

struct Setup {
    MapService svc;
    ServerOptions opt;
    Setup() {
        opt.host = "127.0.0.1";
        opt.port = 0;  // ephemeral
        opt.secret = kSecret;
    }
};

}  // namespace

static void health_is_open() {
    Setup st;
    ServerRunner runner(st.svc, st.opt);
    CHECK(runner.waitReady(3000));
    httplib::Client cli("127.0.0.1", runner.port());
    auto r = cli.Get("/health");
    CHECK(r && r->status == 200);
    CHECK(r->body.find("\"status\":\"ok\"") != std::string::npos);
}

static void missing_credentials_rejected() {
    Setup st;
    ServerRunner runner(st.svc, st.opt);
    CHECK(runner.waitReady(3000));
    httplib::Client cli("127.0.0.1", runner.port());
    auto r = cli.Get("/v1/maps");
    CHECK(r && r->status == 401);
    CHECK(r->body.find("MISSING_CREDENTIALS") != std::string::npos);
}

static void bad_signature_rejected() {
    Setup st;
    ServerRunner runner(st.svc, st.opt);
    CHECK(runner.waitReady(3000));
    httplib::Client cli("127.0.0.1", runner.port());
    auto r = sget(cli, "/v1/maps", "wrong-secret", "n1");
    CHECK(r && r->status == 401);
    CHECK(r->body.find("BAD_SIGNATURE") != std::string::npos);
}

static void stale_timestamp_rejected() {
    Setup st;
    ServerRunner runner(st.svc, st.opt);
    CHECK(runner.waitReady(3000));
    httplib::Client cli("127.0.0.1", runner.port());
    auto r = sget(cli, "/v1/maps", kSecret, "n2", nowTs() - 3600);
    CHECK(r && r->status == 401);
    CHECK(r->body.find("TIMESTAMP_SKEW") != std::string::npos);
}

static void replay_nonce_rejected() {
    Setup st;
    ServerRunner runner(st.svc, st.opt);
    CHECK(runner.waitReady(3000));
    httplib::Client cli("127.0.0.1", runner.port());
    auto r1 = sget(cli, "/v1/maps", kSecret, "nonce-replay");
    CHECK(r1 && r1->status == 200);
    auto r2 = sget(cli, "/v1/maps", kSecret, "nonce-replay");
    CHECK(r2 && r2->status == 401);
    CHECK(r2->body.find("REPLAY_DETECTED") != std::string::npos);
}

static void query_string_canonicalization() {
    // Query order on the wire differs from the canonical (sorted) form;
    // both must authenticate because the server canonicalizes identically.
    Setup st;
    ServerRunner runner(st.svc, st.opt);
    CHECK(runner.waitReady(3000));
    httplib::Client cli("127.0.0.1", runner.port());
    auto cr = spost(cli, "/v1/maps",
                    R"({"width":4,"height":4,"resolution":1.0})",
                    kSecret, "qs-create");
    CHECK(cr && cr->status == 201);
    std::string id = Json::parse(cr->body)["id"].get<std::string>();
    // Wire order: b=...&a=... ; canonical sorts to a then b (unknown map =>
    // 404, but that happens AFTER auth, proving the signature verified).
    std::string target = "/v1/maps/" + id + "/grid?b=1&seq=0";
    auto r = sget(cli, target, kSecret, "qs-1");
    CHECK(r && r->status == 200);
    CHECK(Json::parse(r->body)["seq"] == 0);
}

static void full_workflow() {
    Setup st;
    ServerRunner runner(st.svc, st.opt);
    CHECK(runner.waitReady(3000));
    httplib::Client cli("127.0.0.1", runner.port());

    // create
    std::string create_body =
        R"({"width":8,"height":6,"resolution":0.5,"origin":[-2.0,-1.5]})";
    auto cr = spost(cli, "/v1/maps", create_body, kSecret, "create-1");
    CHECK(cr && cr->status == 201);
    Json cj = Json::parse(cr->body);
    std::string id = cj["id"].get<std::string>();
    CHECK(cj["latest_seq"] == 0);
    CHECK(cj["config"]["resolution"] == 0.5);

    // scan: beam along +x at y=0, a grazing beam, and one no-return beam
    std::string scan_body =
        R"({"pose":[0.0,0.0,0.0],"max_range":5.0,"returns":[
            {"angle":0.0,"range":3.0},
            {"angle":0.2,"range":5.0},
            {"angle":-0.2,"range":-1}
           ]})";
    auto sr = spost(cli, "/v1/maps/" + id + "/scans", scan_body,
                    kSecret, "scan-1");
    CHECK(sr && sr->status == 200);
    Json sj = Json::parse(sr->body);
    CHECK(sj["latest_seq"] == 1);
    std::string v1 = sj["latest_version_digest"];
    std::string state1 = sj["version"]["state_digest"];
    CHECK(state1.size() == 64);

    // Body tampering: signature computed over the original body, but the
    // transmitted bytes are different => 401 BAD_SIGNATURE (map unchanged).
    auto tr = spostTampered(cli, "/v1/maps/" + id + "/scans", scan_body,
                            scan_body + " ", kSecret, "scan-tamper");
    CHECK(tr && tr->status == 401);
    CHECK(tr->body.find("BAD_SIGNATURE") != std::string::npos);

    // A genuinely malformed JSON body with a valid signature => 400.
    auto jr = spost(cli, "/v1/maps/" + id + "/scans", "{not json",
                    kSecret, "scan-badjson");
    CHECK(jr && jr->status == 400);
    CHECK(jr->body.find("INVALID_JSON") != std::string::npos);

    // export grid (latest)
    auto gr = sget(cli, "/v1/maps/" + id + "/grid", kSecret, "grid-1");
    CHECK(gr && gr->status == 200);
    Json gj = Json::parse(gr->body);
    CHECK(gj["state_digest"] == state1);
    bool saw_unknown = false, saw_observed = false;
    for (const auto& row : gj["grid"]["cells"])
        for (const auto& cell : row) {
            if (cell["state"] == "unknown") {
                saw_unknown = true;
                CHECK(cell["p"].is_null());
            } else {
                saw_observed = true;
                CHECK(cell["p"].is_number());
            }
        }
    CHECK(saw_unknown && saw_observed);

    // version export at seq=0: everything unknown
    auto g0r = sget(cli, "/v1/maps/" + id + "/grid?seq=0", kSecret,
                    "grid-v0");
    CHECK(g0r && g0r->status == 200);
    Json g0j = Json::parse(g0r->body);
    int unknown0 = 0;
    for (const auto& row : g0j["grid"]["cells"])
        for (const auto& cell : row)
            if (cell["state"] == "unknown") ++unknown0;
    CHECK(unknown0 == 8 * 6);

    // verify integrity
    auto vr = sget(cli, "/v1/maps/" + id + "/verify", kSecret, "verify-1");
    CHECK(vr && vr->status == 200);
    Json vj = Json::parse(vr->body);
    CHECK(vj["ok"] == true);
    CHECK(vj["latest_version_digest"] == v1);

    // unknown map => 404 (after auth)
    auto nfr = sget(cli, "/v1/maps/does-not-exist/grid", kSecret, "nf-1");
    CHECK(nfr && nfr->status == 404);
}

static void param_mismatch_rejected() {
    Setup st;
    ServerRunner runner(st.svc, st.opt);
    CHECK(runner.waitReady(3000));
    httplib::Client cli("127.0.0.1", runner.port());

    auto cr = spost(cli, "/v1/maps",
                    R"({"width":8,"height":6,"resolution":0.5,
                        "origin":[-2.0,-1.5]})",
                    kSecret, "pm-create");
    CHECK(cr && cr->status == 201);
    std::string id = Json::parse(cr->body)["id"].get<std::string>();

    auto sr = spost(
        cli, "/v1/maps/" + id + "/scans",
        R"({"pose":[0,0,0],"resolution":0.25,"max_range":5,
            "returns":[{"angle":0,"range":3}]})",
        kSecret, "pm-scan-res");
    CHECK(sr && sr->status == 409);
    CHECK(sr->body.find("PARAM_MISMATCH") != std::string::npos);

    auto sr2 = spost(
        cli, "/v1/maps/" + id + "/scans",
        R"({"pose":[0,0,0],"grid_origin":[0,0],"max_range":5,
            "returns":[{"angle":0,"range":3}]})",
        kSecret, "pm-scan-org");
    CHECK(sr2 && sr2->status == 409);
    CHECK(sr2->body.find("PARAM_MISMATCH") != std::string::npos);
}

int main() {
    RUN_TEST(health_is_open);
    RUN_TEST(missing_credentials_rejected);
    RUN_TEST(bad_signature_rejected);
    RUN_TEST(stale_timestamp_rejected);
    RUN_TEST(replay_nonce_rejected);
    RUN_TEST(query_string_canonicalization);
    RUN_TEST(full_workflow);
    RUN_TEST(param_mismatch_rejected);
    return TEST_MAIN_RETURN();
}
