// solver_service.cpp - JSON request validation, solving and response assembly.
#include "solver_service.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "diff_constraints.hpp"

namespace diffcon {
namespace {

using json::Value;

struct ParsedSystem {
  std::vector<std::string> names;
  std::unordered_map<std::string, int> index;
  std::vector<Constraint> edges;
  std::vector<std::string> edgeIds;
};

Value errorResponse(const std::string& code, const std::string& message) {
  Value root = Value::makeObject();
  root.set("status", "error");
  Value err = Value::makeObject();
  err.set("code", code);
  err.set("message", message);
  root.set("error", err);
  return root;
}

int internVariable(ParsedSystem& sys, const std::string& name) {
  auto it = sys.index.find(name);
  if (it != sys.index.end()) return it->second;
  int id = static_cast<int>(sys.names.size());
  sys.index.emplace(name, id);
  sys.names.push_back(name);
  return id;
}

// Reads an integer JSON value, rejecting floating-point values and out-of-range c.
bool readInt64(const Value& v, int64_t& out) {
  if (v.isInt()) {
    out = v.asInt();
    return true;
  }
  return false;  // doubles, strings, bools are not accepted
}

Value intArray(const std::vector<int64_t>& values) {
  Value arr = Value::makeArray();
  for (int64_t v : values) arr.pushBack(Value(v));
  return arr;
}

Value strArray(const std::vector<std::string>& values) {
  Value arr = Value::makeArray();
  for (const auto& s : values) arr.pushBack(Value(s));
  return arr;
}

Value nameArray(const std::vector<int>& indices, const std::vector<std::string>& names) {
  Value arr = Value::makeArray();
  for (int i : indices) arr.pushBack(Value(names[i]));
  return arr;
}

Value idArray(const std::vector<int>& edgeIndices, const std::vector<std::string>& ids) {
  Value arr = Value::makeArray();
  for (int e : edgeIndices) arr.pushBack(Value(ids[e]));
  return arr;
}

// Run Bellman-Ford on a subset of constraints (used for candidate verification).
BellmanFordResult runOnSubset(int n, const std::vector<Constraint>& edges,
                              const std::vector<int>& subset) {
  std::vector<Constraint> sub;
  sub.reserve(subset.size());
  for (int e : subset) sub.push_back(edges[e]);
  return bellmanFord(n, sub);
}

}  // namespace

Value handleSolveRequest(const std::string& body) {
  Value request;
  try {
    request = Value::parse(body);
  } catch (const json::ParseError& e) {
    return errorResponse("invalid_json", std::string("Request body is not valid JSON: ") + e.what());
  }
  if (!request.isObject())
    return errorResponse("invalid_request", "Top-level JSON value must be an object.");

  // ---- Options ----
  bool normalize = true;
  bool wantMinimal = true;
  std::string referenceMode = "auto";
  if (const Value* opts = request.find("options")) {
    if (!opts->isObject()) return errorResponse("invalid_options", "\"options\" must be an object.");
    if (const Value* v = opts->find("normalize")) {
      if (!v->isBool()) return errorResponse("invalid_options", "options.normalize must be a boolean.");
      normalize = v->asBool();
    }
    if (const Value* v = opts->find("minimal_candidate")) {
      if (!v->isBool())
        return errorResponse("invalid_options", "options.minimal_candidate must be a boolean.");
      wantMinimal = v->asBool();
    }
    if (const Value* v = opts->find("reference")) {
      if (!v->isString())
        return errorResponse("invalid_options", "options.reference must be \"auto\", \"on\" or \"off\".");
      referenceMode = v->asString();
      if (referenceMode != "auto" && referenceMode != "on" && referenceMode != "off")
        return errorResponse("invalid_options", "options.reference must be \"auto\", \"on\" or \"off\".");
    }
  }

  // ---- Declared variables ----
  ParsedSystem sys;
  if (const Value* vars = request.find("variables")) {
    if (!vars->isArray()) return errorResponse("invalid_variables", "\"variables\" must be an array.");
    if (static_cast<int>(vars->asArray().size()) > MAX_VARIABLES)
      return errorResponse("too_many_variables",
                           "Too many variables (limit " + std::to_string(MAX_VARIABLES) + ").");
    std::unordered_set<std::string> seen;
    for (const Value& v : vars->asArray()) {
      if (!v.isString())
        return errorResponse("invalid_variables", "Every variable name must be a string.");
      const std::string& name = v.asString();
      if (name.empty()) return errorResponse("invalid_variables", "Variable names must not be empty.");
      if (!seen.insert(name).second)
        return errorResponse("duplicate_variable", "Duplicate variable name: " + name);
      internVariable(sys, name);
    }
  }

  // ---- Constraints ----
  const Value* cons = request.find("constraints");
  if (!cons) return errorResponse("missing_constraints", "Request must contain a \"constraints\" array.");
  if (!cons->isArray()) return errorResponse("invalid_constraints", "\"constraints\" must be an array.");
  if (static_cast<int>(cons->asArray().size()) > MAX_CONSTRAINTS)
    return errorResponse("too_many_constraints",
                         "Too many constraints (limit " + std::to_string(MAX_CONSTRAINTS) + ").");

  std::unordered_set<std::string> seenIds;
  int idx = 0;
  for (const Value& item : cons->asArray()) {
    if (!item.isObject())
      return errorResponse("invalid_constraint",
                           "Constraint at index " + std::to_string(idx) + " must be an object.");
    const Value* xv = item.find("x");
    const Value* yv = item.find("y");
    const Value* cv = item.find("c");
    if (!xv || !yv || !cv)
      return errorResponse(
          "invalid_constraint",
          "Constraint at index " + std::to_string(idx) + " requires fields x, y and c (x - y <= c).");
    if (!xv->isString() || !yv->isString())
      return errorResponse("invalid_constraint",
                           "Constraint at index " + std::to_string(idx) + ": x and y must be strings.");
    int64_t c;
    if (!readInt64(*cv, c))
      return errorResponse(
          "invalid_constraint",
          "Constraint at index " + std::to_string(idx) + ": c must be an integer (floats are rejected).");
    if (c < -MAX_ABS_C || c > MAX_ABS_C)
      return errorResponse("constraint_out_of_range",
                           "Constraint at index " + std::to_string(idx) +
                               ": |c| must be <= " + std::to_string(MAX_ABS_C) + ".");

    std::string id = std::to_string(idx);
    if (const Value* idv = item.find("id")) {
      if (!idv->isString())
        return errorResponse("invalid_constraint",
                             "Constraint at index " + std::to_string(idx) + ": id must be a string.");
      id = idv->asString();
    }
    // Covers both explicit ids and auto-generated index ids, so an explicit
    // "3" cannot collide with the id generated for an unlabelled constraint.
    if (!seenIds.insert(id).second)
      return errorResponse("duplicate_constraint_id", "Duplicate constraint id: " + id);

    int xi = internVariable(sys, xv->asString());
    int yi = internVariable(sys, yv->asString());
    if (static_cast<int>(sys.names.size()) > MAX_VARIABLES)
      return errorResponse("too_many_variables",
                           "Too many variables (limit " + std::to_string(MAX_VARIABLES) + ").");
    sys.edges.push_back(Constraint{xi, yi, c});
    sys.edgeIds.push_back(id);
    ++idx;
  }

  const int n = static_cast<int>(sys.names.size());
  const int m = static_cast<int>(sys.edges.size());

  // ---- Solve ----
  auto t0 = std::chrono::steady_clock::now();
  BellmanFordResult bf = bellmanFord(n, sys.edges);
  auto t1 = std::chrono::steady_clock::now();
  int64_t bfMicros = std::chrono::duration_cast<std::chrono::microseconds>(t1 - t0).count();

  auto comps = connectedComponents(n, sys.edges);

  Value root = Value::makeObject();
  root.set("status", "ok");
  root.set("feasible", bf.feasible);
  root.set("variables", strArray(sys.names));
  Value sizes = Value::makeObject();
  sizes.set("num_variables", n);
  sizes.set("num_constraints", m);
  sizes.set("num_components", static_cast<int64_t>(comps.size()));
  root.set("sizes", sizes);

  // Evidence from the primary algorithm.
  Value evidence = Value::makeObject();
  evidence.set("primary_algorithm", "bellman_ford");
  evidence.set("note", "Labels start at 0: an implicit 0-weight super-source reaches every component.");
  evidence.set("iterations", bf.iterations);
  evidence.set("relaxations", static_cast<int64_t>(bf.relaxations));
  evidence.set("elapsed_micros", bfMicros);

  // Naive O(n^3) Floyd-Warshall reference cross-check.
  bool useReference = (referenceMode == "on") || (referenceMode == "auto" && n <= NAIVE_FLOYD_N_LIMIT);
  Value ref = Value::makeObject();
  if (useReference) {
    if (referenceMode == "on" && n > NAIVE_FLOYD_N_LIMIT) {
      ref.set("used", false);
      ref.set("reason", "reference forced on but n exceeds Floyd limit " +
                            std::to_string(NAIVE_FLOYD_N_LIMIT));
    } else {
      FloydResult fw = floydWarshall(n, sys.edges);
      ref.set("used", true);
      ref.set("algorithm", "floyd_warshall");
      ref.set("elapsed_micros", static_cast<int64_t>(fw.elapsedMicros));
      ref.set("verdict_matches_primary", fw.feasible == bf.feasible);
      if (fw.feasible == bf.feasible && fw.feasible) {
        int64_t violating = -1;
        bool fwOk = assignmentSatisfies(fw.dist, sys.edges, &violating);
        ref.set("floyd_assignment_verified", fwOk);
      } else {
        ref.set("floyd_assignment_verified", Value());  // null: no assignment exists
        ref.set("note", "Infeasible systems admit no assignment to verify.");
      }
    }
  } else {
    ref.set("used", false);
    ref.set("reason", referenceMode == "off"
                         ? "reference disabled by options.reference=off"
                         : "n exceeds Floyd O(n^3) limit " + std::to_string(NAIVE_FLOYD_N_LIMIT));
  }
  evidence.set("reference", ref);
  root.set("evidence", evidence);

  Value componentsOut = Value::makeArray();
  for (const auto& comp : comps) componentsOut.pushBack(nameArray(comp, sys.names));

  if (bf.feasible) {
    std::vector<int64_t> labels = bf.dist;
    std::string normMode = "raw_bellman_ford_labels";
    if (normalize) {
      labels = normalizePerComponent(labels, comps);
      normMode = "per_component_min_zero";
    }
    int64_t violating = -1;
    bool ok = assignmentSatisfies(labels, sys.edges, &violating);

    Value assignment = Value::makeObject();
    for (int v = 0; v < n; ++v) assignment.set(sys.names[v], Value(labels[v]));
    root.set("assignment", assignment);

    Value norm = Value::makeObject();
    norm.set("mode", normMode);
    norm.set("components", componentsOut);
    root.set("normalization", norm);

    Value verification = Value::makeObject();
    verification.set("all_satisfied", ok);
    verification.set("checked_constraints", static_cast<int64_t>(m));
    if (!ok) verification.set("first_violating_constraint", Value(sys.edgeIds[violating]));
    root.set("verification", verification);
  } else {
    // ---- Negative-cycle witness ----
    int64_t sum = 0;
    for (int e : bf.cycleEdges) sum += sys.edges[e].c;
    Value cycle = Value::makeObject();
    cycle.set("constraint_ids", idArray(bf.cycleEdges, sys.edgeIds));
    cycle.set("edge_indices", intArray(std::vector<int64_t>(bf.cycleEdges.begin(),
                                                            bf.cycleEdges.end())));
    cycle.set("variable_cycle", nameArray(bf.cycleVertices, sys.names));
    cycle.set("length", static_cast<int64_t>(bf.cycleEdges.size()));
    cycle.set("sum_bounds", sum);
    cycle.set("closes", bf.cycleVertices.front() == bf.cycleVertices.back());
    cycle.set("explanation",
              "Each constraint x - y <= c is the edge y -> x of weight c. Traversing the "
              "cycle and summing telescopes the variable differences to 0, so the "
              "constraints imply 0 <= " + std::to_string(sum) + ", which is impossible.");
    root.set("negative_cycle", cycle);

    if (wantMinimal) {
      auto mc = minimalContradictionCandidate(n, sys.edges, bf.cycleEdges);
      Value candidate = Value::makeObject();
      candidate.set("method", mc.method);
      candidate.set("constraint_ids", idArray(mc.edgeIndices, sys.edgeIds));
      candidate.set("size", static_cast<int64_t>(mc.edgeIndices.size()));

      // Independent verification of the candidate on top of the builder's checks.
      BellmanFordResult sub = runOnSubset(n, sys.edges, mc.edgeIndices);
      bool irreducible = true;
      if (!sub.feasible) {
        for (int e : mc.edgeIndices) {
          std::vector<int> reduced;
          reduced.reserve(mc.edgeIndices.size());
          for (int f : mc.edgeIndices)
            if (f != e) reduced.push_back(f);
          BellmanFordResult oneLess = runOnSubset(n, sys.edges, reduced);
          if (!oneLess.feasible) { irreducible = false; break; }
        }
      }
      Value mcVerify = Value::makeObject();
      mcVerify.set("subset_infeasible", !sub.feasible);
      mcVerify.set("irreducible", !sub.feasible && irreducible);
      mcVerify.set("definition", "Minimal = removing any single member makes the rest feasible; "
                                 "it is not necessarily the globally smallest such set.");
      candidate.set("verification", mcVerify);
      root.set("minimal_candidate", candidate);
    }
  }

  return root;
}

}  // namespace diffcon
