#include "report.h"

#include <set>

#include "canonical.h"
#include "crypto.h"
#include "util.h"

namespace pgo {

using nlohmann::json;

namespace {

json EdgeErrorJson(const EdgeError& e) {
  return json{
      {"edge_id", e.edge_id},
      {"from", e.from},
      {"to", e.to},
      {"error", {e.raw_error[0], e.raw_error[1], e.raw_error[2]}},
      {"raw_norm", e.raw_norm},
      {"weighted_squared", e.weighted_squared},
      {"robust_cost", e.robust_cost},
  };
}

json CostJson(const CostSummary& c) {
  return json{
      {"weighted_cost", c.weighted_cost},
      {"raw_squared", c.raw_squared},
      {"robust_cost", c.robust_cost},
  };
}

}  // namespace

nlohmann::json BuildReport(const ReportInputs& in) {
  const Graph& g = *in.graph;
  const OptimizeOutcome& o = *in.outcome;
  const RunRecord& r = in.run;

  std::set<std::string> anchor_set(in.anchors->begin(), in.anchors->end());

  json nodes = json::array();
  for (size_t i = 0; i < o.node_order.size(); ++i) {
    const std::string& id = o.node_order[i];
    const Node& n = g.nodes.at(id);
    const Pose& fin = o.optimized[i];
    nodes.push_back(json{
        {"id", id},
        {"anchor", anchor_set.count(id) > 0},
        {"init", {n.init.x, n.init.y, n.init.theta}},
        {"final", {fin.x, fin.y, NormalizeAngle(fin.theta)}},
    });
  }

  json initial_edges = json::array();
  json final_edges = json::array();
  for (const auto& e : o.initial_errors) initial_edges.push_back(EdgeErrorJson(e));
  for (const auto& e : o.final_errors) final_edges.push_back(EdgeErrorJson(e));

  json body = {
      {"protocol", kResultProtocol},
      {"version", "1.0"},
      {"graph_version", r.graph_version},
      {"input_sha256", r.input_sha256},
      {"run_id", r.run_id},
      {"created_at", r.created_at},
      {"tool",
       {
           {"name", "pgo-se2"},
           {"version", r.tool_version},
           {"ceres_version", r.ceres_version},
       }},
      {"status", r.status},
      {"exit_reason", r.exit_reason},
      {"summary",
       {
           {"num_nodes", r.num_nodes},
           {"num_edges", r.num_edges},
           {"num_components", r.num_components},
           {"anchors", json::parse(r.anchors_json)},
       }},
      {"residual",
       {
           {"initial", CostJson(o.initial)},
           {"final", CostJson(o.final)},
       }},
      {"solve",
       {
           {"iterations", r.iterations},
           {"max_iterations", r.max_iterations},
           {"elapsed_ms", r.elapsed_ms},
           {"termination", o.termination},
           {"message", r.message},
       }},
      {"nodes", nodes},
      {"edges",
       {
           {"initial", initial_edges},
           {"final", final_edges},
       }},
  };

  std::string body_canon = CanonicalJson(body);
  std::string body_hash = Sha256Hex(body_canon);

  json manifest = {
      {"body_canonical_form",
       "sorted-key JSON, 2-space indent, 17-digit doubles"},
      {"body_sha256", body_hash},
      {"hash_algorithm", "SHA-256"},
      {"signed", false},
  };
  if (!in.signing_key.empty()) {
    std::string mac;
    std::string err;
    HmacSha256Hex(in.signing_key, body_hash, &mac, &err);
    manifest["signed"] = true;
    manifest["hmac_algorithm"] = "HMAC-SHA-256";
    manifest["hmac_message"] = "body_sha256";
    manifest["hmac_sha256"] = mac;
  }

  return json{{"protocol", kResultProtocol},
              {"version", "1.0"},
              {"body", body},
              {"manifest", manifest}};
}

VerifyResult VerifyReport(const std::string& json_text,
                          const std::string& expected_key) {
  VerifyResult vr;
  json doc;
  try {
    doc = json::parse(json_text);
  } catch (const std::exception& e) {
    vr.status = "malformed";
    vr.message = std::string("parse failed: ") + e.what();
    return vr;
  }
  if (!doc.is_object() || !doc.contains("body") || !doc.contains("manifest") ||
      !doc["body"].is_object() || !doc["manifest"].is_object()) {
    vr.status = "malformed";
    vr.message = "document must contain protocol/body/manifest";
    return vr;
  }
  if (!doc["body"].contains("protocol") ||
      doc["body"]["protocol"].get<std::string>() != kResultProtocol) {
    vr.status = "malformed";
    vr.message = "unsupported body protocol";
    return vr;
  }
  const json& man = doc["manifest"];
  if (!man.contains("body_sha256") || !man["body_sha256"].is_string()) {
    vr.status = "malformed";
    vr.message = "manifest.body_sha256 missing";
    return vr;
  }
  std::string want = man["body_sha256"].get<std::string>();
  std::string got = Sha256Hex(CanonicalJson(doc["body"]));
  vr.body_sha256 = got;
  if (!ConstantTimeEquals(got, want)) {
    vr.status = "tampered";
    vr.message = "body content hash mismatch — file was modified";
    return vr;
  }

  bool has_mac = man.value("signed", false) && man.contains("hmac_sha256");
  vr.signed_report = has_mac;
  if (has_mac) {
    if (expected_key.empty()) {
      vr.status = "bad_signature";
      vr.message =
          "report is HMAC-signed; verify with --signing-key <key-file|env:...>";
      return vr;
    }
    std::string mac, err;
    if (!HmacSha256Hex(expected_key, want, &mac, &err)) {
      vr.status = "bad_signature";
      vr.message = err;
      return vr;
    }
    if (!ConstantTimeEquals(mac, man["hmac_sha256"].get<std::string>())) {
      vr.status = "bad_signature";
      vr.message = "HMAC mismatch — wrong key or altered file";
      return vr;
    }
  } else if (!expected_key.empty()) {
    vr.status = "bad_signature";
    vr.message = "key supplied but document is unsigned";
    return vr;
  }

  vr.ok = true;
  vr.status = "ok";
  vr.message = has_mac ? "valid: SHA-256 and HMAC-SHA-256 verified"
                       : "valid: SHA-256 verified (unsigned)";
  return vr;
}

}  // namespace pgo
