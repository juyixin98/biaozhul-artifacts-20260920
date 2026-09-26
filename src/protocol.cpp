#include "protocol.hpp"

#include <set>
#include <sstream>
#include <string>
#include <vector>

#include "bruteforce.hpp"
#include "limits.hpp"
#include "maxflow.hpp"
#include "mincut.hpp"
#include "verifier.hpp"

namespace mcut {

namespace {

const JsonValue* req_field(const JsonValue& req, const std::string& key) {
  const JsonValue* v = req.find(key);
  if (v == nullptr || v->is(JsonValue::Type::kNull)) return nullptr;
  return v;
}

std::string int_to_str(std::int64_t x) {
  std::ostringstream os;
  os << x;
  return os.str();
}

JsonValue edge_flow_json(const InputEdge& e, std::int64_t flow) {
  JsonValue::Object o;
  o.emplace("id", JsonValue::string(e.id));
  o.emplace("from", JsonValue::integer(e.from));
  o.emplace("to", JsonValue::integer(e.to));
  o.emplace("capacity", JsonValue::integer(e.capacity));
  o.emplace("flow", JsonValue::integer(flow));
  return JsonValue::object(std::move(o));
}

}  // namespace

JsonValue error_response(const std::string& code, const std::string& message) {
  JsonValue::Object o;
  o.emplace("status", JsonValue::string("error"));
  o.emplace("error_code", JsonValue::string(code));
  o.emplace("message", JsonValue::string(message));
  return JsonValue::object(std::move(o));
}

JsonValue handle_request(const JsonValue& request, RequestError& error) {
  if (!request.is(JsonValue::Type::kObject)) {
    error = {"INVALID_REQUEST", "request body must be a JSON object"};
    return error_response(error.code, error.message);
  }

  // ---- num_vertices ----
  Problem problem;
  {
    const JsonValue* v = req_field(request, "num_vertices");
    if (v == nullptr || !v->is(JsonValue::Type::kInt)) {
      error = {"INVALID_REQUEST", "field 'num_vertices' must be an integer"};
      return error_response(error.code, error.message);
    }
    std::int64_t n = v->as_int();
    if (n < 2 || n > kMaxVertices) {
      error = {"SCALE_LIMIT",
               "num_vertices must be in [2, " + int_to_str(kMaxVertices) +
                   "], got " + int_to_str(n)};
      return error_response(error.code, error.message);
    }
    problem.num_vertices = static_cast<int>(n);
  }

  // ---- source / sink ----
  for (const char* name : {"source", "sink"}) {
    const JsonValue* v = req_field(request, name);
    if (v == nullptr || !v->is(JsonValue::Type::kInt)) {
      error = {"INVALID_REQUEST",
               std::string("field '") + name + "' must be an integer"};
      return error_response(error.code, error.message);
    }
    std::int64_t x = v->as_int();
    if (x < 0 || x >= problem.num_vertices) {
      error = {"INVALID_REQUEST",
               std::string(name) + " vertex " + int_to_str(x) +
                   " out of range [0, " +
                   int_to_str(problem.num_vertices - 1) + "]"};
      return error_response(error.code, error.message);
    }
    if (std::string(name) == "source") problem.source = static_cast<int>(x);
    else problem.sink = static_cast<int>(x);
  }
  if (problem.source == problem.sink) {
    error = {"INVALID_REQUEST", "source and sink must be distinct vertices"};
    return error_response(error.code, error.message);
  }

  // ---- edges ----
  {
    const JsonValue* v = req_field(request, "edges");
    if (v == nullptr) {
      error = {"INVALID_REQUEST", "field 'edges' must be an array"};
      return error_response(error.code, error.message);
    }
    if (!v->is(JsonValue::Type::kArray)) {
      error = {"INVALID_REQUEST", "field 'edges' must be an array"};
      return error_response(error.code, error.message);
    }
    const auto& arr = v->as_array();
    if (static_cast<int>(arr.size()) > kMaxEdges) {
      error = {"SCALE_LIMIT",
               "too many edges: " + int_to_str(arr.size()) + " > limit " +
                   int_to_str(kMaxEdges)};
      return error_response(error.code, error.message);
    }
    std::set<std::string> seen_ids;
    for (size_t i = 0; i < arr.size(); ++i) {
      const JsonValue& je = arr[i];
      std::string ctx = "edges[" + std::to_string(i) + "]";
      if (!je.is(JsonValue::Type::kObject)) {
        error = {"INVALID_REQUEST", ctx + " must be an object"};
        return error_response(error.code, error.message);
      }
      InputEdge e;

      const JsonValue* idv = je.find("id");
      if (idv == nullptr || !idv->is(JsonValue::Type::kString) ||
          idv->as_string().empty()) {
        error = {"INVALID_REQUEST", ctx + ".id must be a nonempty string"};
        return error_response(error.code, error.message);
      }
      e.id = idv->as_string();
      if (!seen_ids.insert(e.id).second) {
        error = {"INVALID_REQUEST",
                 "duplicate edge id '" + e.id + "'"};
        return error_response(error.code, error.message);
      }

      for (const char* name : {"from", "to"}) {
        const JsonValue* xv = je.find(name);
        if (xv == nullptr || !xv->is(JsonValue::Type::kInt)) {
          error = {"INVALID_REQUEST",
                   ctx + "." + name + " must be an integer"};
          return error_response(error.code, error.message);
        }
        std::int64_t x = xv->as_int();
        if (x < 0 || x >= problem.num_vertices) {
          error = {"INVALID_REQUEST",
                   ctx + "." + name + " vertex " + int_to_str(x) +
                       " out of range"};
          return error_response(error.code, error.message);
        }
        if (std::string(name) == "from") e.from = static_cast<int>(x);
        else e.to = static_cast<int>(x);
      }

      const JsonValue* cv = je.find("capacity");
      if (cv == nullptr || !cv->is(JsonValue::Type::kInt)) {
        error = {"INVALID_REQUEST",
                 ctx + ".capacity must be a nonnegative integer"};
        return error_response(error.code, error.message);
      }
      std::int64_t c = cv->as_int();
      if (c < 0) {
        error = {"INVALID_REQUEST",
                 ctx + ".capacity must be nonnegative, got " + int_to_str(c)};
        return error_response(error.code, error.message);
      }
      if (c > kMaxCapacity) {
        error = {"SCALE_LIMIT",
                 ctx + ".capacity " + int_to_str(c) + " exceeds limit " +
                     int_to_str(kMaxCapacity)};
        return error_response(error.code, error.message);
      }
      e.capacity = c;
      problem.edges.push_back(std::move(e));
    }
  }

  // ---- optional bruteforce flag ----
  bool want_brute = false;
  if (const JsonValue* v = request.find("bruteforce");
      v != nullptr && !v->is(JsonValue::Type::kNull)) {
    if (!v->is(JsonValue::Type::kBool)) {
      error = {"INVALID_REQUEST", "field 'bruteforce' must be a boolean"};
      return error_response(error.code, error.message);
    }
    want_brute = v->as_bool();
    if (want_brute &&
        (problem.num_vertices > kBruteMaxVertices ||
         static_cast<int>(problem.edges.size()) > kBruteMaxEdges)) {
      error = {"BRUTE_TOO_LARGE",
               "bruteforce reference requires num_vertices <= " +
                   int_to_str(kBruteMaxVertices) + " and edges <= " +
                   int_to_str(kBruteMaxEdges)};
      return error_response(error.code, error.message);
    }
  }

  // ---- solve (Dinic, implemented from scratch in maxflow.cpp) ----
  Dinic dinic(problem.num_vertices);
  for (int i = 0; i < static_cast<int>(problem.edges.size()); ++i) {
    const InputEdge& e = problem.edges[i];
    dinic.add_original_edge(i, e.from, e.to, e.capacity);
  }
  std::int64_t flow_value =
      dinic.compute_max_flow(problem.source, problem.sink);

  std::vector<std::int64_t> flows(problem.edges.size());
  for (int i = 0; i < static_cast<int>(problem.edges.size()); ++i) {
    flows[i] = dinic.edge_flow(i);
  }

  CutCertificate cut = build_min_cut(problem, dinic, problem.source);

  // ---- independent verification ----
  VerifyReport report =
      verify_solution(problem, flows, cut.source_side, flow_value);

  // ---- assemble response ----
  JsonValue::Object root;
  root.emplace("status", JsonValue::string(report.ok ? "ok" : "error"));

  JsonValue::Object graph_echo;
  graph_echo.emplace("num_vertices",
                     JsonValue::integer(problem.num_vertices));
  graph_echo.emplace("source", JsonValue::integer(problem.source));
  graph_echo.emplace("sink", JsonValue::integer(problem.sink));
  root.emplace("input", JsonValue::object(std::move(graph_echo)));

  root.emplace("max_flow_value", JsonValue::integer(flow_value));

  JsonValue::Array flow_arr;
  for (int i = 0; i < static_cast<int>(problem.edges.size()); ++i) {
    flow_arr.push_back(edge_flow_json(problem.edges[i], flows[i]));
  }
  root.emplace("flow", JsonValue::array(std::move(flow_arr)));

  JsonValue::Array side_s;
  JsonValue::Array side_t;
  for (int v = 0; v < problem.num_vertices; ++v) {
    if (cut.source_side[v]) side_s.push_back(JsonValue::integer(v));
    else side_t.push_back(JsonValue::integer(v));
  }
  JsonValue::Array cut_ids;
  JsonValue::Array cut_arr;
  for (const CutEdgeInfo& ce : cut.cut_edges) {
    const InputEdge& e = problem.edges[ce.edge_id];
    cut_ids.push_back(JsonValue::string(e.id));
    cut_arr.push_back(edge_flow_json(e, ce.flow));
  }
  JsonValue::Object cut_obj;
  cut_obj.emplace("source_side", JsonValue::array(std::move(side_s)));
  cut_obj.emplace("sink_side", JsonValue::array(std::move(side_t)));
  cut_obj.emplace("cut_edge_ids", JsonValue::array(std::move(cut_ids)));
  cut_obj.emplace("cut_edges", JsonValue::array(std::move(cut_arr)));
  cut_obj.emplace("cut_value", JsonValue::integer(cut.cut_value));
  root.emplace("min_cut", JsonValue::object(std::move(cut_obj)));

  JsonValue::Array checks_arr;
  for (const auto& [name, passed] : report.checks) {
    JsonValue::Object c;
    c.emplace("name", JsonValue::string(name));
    c.emplace("passed", JsonValue::boolean(passed));
    checks_arr.push_back(JsonValue::object(std::move(c)));
  }
  JsonValue::Object ver;
  ver.emplace("ok", JsonValue::boolean(report.ok));
  ver.emplace("checks", JsonValue::array(std::move(checks_arr)));
  JsonValue::Array fail_arr;
  for (const std::string& f : report.failures) {
    fail_arr.push_back(JsonValue::string(f));
  }
  ver.emplace("failures", JsonValue::array(std::move(fail_arr)));
  ver.emplace("source_outflow", JsonValue::integer(report.source_outflow));
  ver.emplace("sink_inflow", JsonValue::integer(report.sink_inflow));
  ver.emplace("cut_value", JsonValue::integer(report.cut_value));
  root.emplace("verification", JsonValue::object(std::move(ver)));

  if (want_brute) {
    BruteForceResult bf = brute_force_min_cut(problem);
    JsonValue::Object bfo;
    bfo.emplace("enabled", JsonValue::boolean(true));
    bfo.emplace("partitions_checked",
                JsonValue::integer(static_cast<std::int64_t>(
                    bf.partitions_checked)));
    bfo.emplace("min_cut_value",
                JsonValue::integer(bf.min_cut_value));
    bfo.emplace("matches_max_flow",
                JsonValue::boolean(bf.min_cut_value == flow_value &&
                                   bf.min_cut_value == cut.cut_value));
    JsonValue::Array bf_s;
    for (int v = 0; v < problem.num_vertices; ++v) {
      if (bf.source_side[v]) bf_s.push_back(JsonValue::integer(v));
    }
    bfo.emplace("reference_source_side", JsonValue::array(std::move(bf_s)));
    root.emplace("bruteforce", JsonValue::object(std::move(bfo)));
  }

  if (!report.ok) {
    // Solver produced an internally invalid answer: this must never happen,
    // but surface it loudly instead of pretending success.
    root.emplace("error_code", JsonValue::string("SOLVER_SELF_CHECK_FAILED"));
    JsonValue::Array self_fail_arr;
    for (const std::string& f : report.failures) {
      self_fail_arr.push_back(JsonValue::string(f));
    }
    root.emplace("failures", JsonValue::array(std::move(self_fail_arr)));
    root.emplace("message",
                 JsonValue::string("internal verification failed"));
  }

  return JsonValue::object(std::move(root));
}

}  // namespace mcut
