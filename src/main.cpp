// diffc - difference-constraints diagnosis backend (CLI / JSON over stdio).
//
// Usage:
//   diffc solve     < request.json   feasibility + normalized assignment,
//                                    or negative cycle with constraint ids
//   diffc verify    < request.json   check a supplied assignment, list
//                                    violated constraint ids
//   diffc diagnose  < request.json   minimal infeasible subset candidates
//   diffc selftest [rounds] [seed]   randomized cross-check of solver vs
//                                    naive reference (no input needed)
//
// Exit codes: 0 ok (including "infeasible" answers), 1 input/usage error,
//             2 selftest failure.
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <iostream>
#include <random>
#include <sstream>
#include <string>

#include "diagnose.hpp"
#include "json.hpp"
#include "model.hpp"
#include "reference.hpp"
#include "solver.hpp"

namespace {

using namespace diffc;

std::string readAllStdin() {
  std::ostringstream ss;
  ss << std::cin.rdbuf();
  return ss.str();
}

Json constraintIdsToJson(const Problem& p, const std::vector<int>& ids) {
  Json arr = Json::makeArr();
  for (int i : ids) arr.arr.push_back(Json::makeStr(p.constraints[i].id));
  return arr;
}

Json assignmentToJson(const Problem& p, const std::vector<std::int64_t>& a) {
  Json obj = Json::makeObj();
  for (size_t i = 0; i < p.varNames.size(); ++i)
    obj.set(p.varNames[i], Json::makeInt(a[i]));
  return obj;
}

// Builds the self-check evidence block: re-verify the assignment we are
// about to report, so every feasible answer carries machine-checkable proof.
Json verificationBlock(const Problem& p, const std::vector<std::int64_t>& a) {
  Json v = Json::makeObj();
  std::vector<int> bad = findViolations(p, a);
  v.set("satisfied", Json::makeBool(bad.empty()));
  v.set("violated", constraintIdsToJson(p, bad));
  return v;
}

int cmdSolve(const Json& root) {
  Problem p = parseProblem(root);
  SolveResult r = solve(p);
  Json out = Json::makeObj();

  if (r.feasible) {
    out.set("status", Json::makeStr("feasible"));
    out.set("assignment", assignmentToJson(p, r.assignment));
    out.set("normalization",
            Json::makeStr("translated so that min(assignment) == 0"));
    out.set("verification", verificationBlock(p, r.assignment));
  } else {
    Json cycle = Json::makeObj();
    cycle.set("constraint_ids", constraintIdsToJson(p, r.cycleConstraints));
    Json vars = Json::makeArr();
    for (int v : r.cycleVariables)
      vars.arr.push_back(Json::makeStr(p.varNames[v]));
    cycle.set("variables", vars);
    cycle.set("total_weight", Json::makeInt(r.cycleWeight));
    cycle.set("explanation",
              Json::makeStr("bounds along this constraint cycle sum to " +
                            std::to_string(r.cycleWeight) + " < 0"));

    out.set("status", Json::makeStr("infeasible"));
    out.set("negative_cycle", cycle);
    // The cycle's constraints alone are a minimal infeasible subset.
    std::vector<int> ids = r.cycleConstraints;
    std::sort(ids.begin(), ids.end());
    out.set("minimal_infeasible_subset", constraintIdsToJson(p, ids));
  }
  std::cout << out.dump();
  return 0;
}

int cmdVerify(const Json& root) {
  Problem p = parseProblem(root);
  const Json* aj = root.find("assignment");
  if (!aj || aj->type != Json::Type::Obj)
    throw InputError{"\"assignment\" must be an object mapping variable names to integers"};

  std::vector<std::int64_t> a(p.varNames.size(), 0);
  for (size_t i = 0; i < p.varNames.size(); ++i) {
    const Json* v = aj->find(p.varNames[i]);
    if (!v) throw InputError{"assignment missing variable \"" + p.varNames[i] + "\""};
    if (v->type != Json::Type::Int)
      throw InputError{"assignment for \"" + p.varNames[i] + "\" must be an integer"};
    a[i] = v->integer;
  }

  std::vector<int> bad = findViolations(p, a);
  Json out = Json::makeObj();
  out.set("satisfied", Json::makeBool(bad.empty()));
  out.set("violated", constraintIdsToJson(p, bad));
  std::cout << out.dump();
  return 0;
}

int cmdDiagnose(const Json& root) {
  Problem p = parseProblem(root);
  DiagnoseResult d = diagnose(p);
  Json out = Json::makeObj();
  out.set("status", Json::makeStr(d.feasible ? "feasible" : "infeasible"));

  Json cands = Json::makeArr();
  for (const auto& c : d.candidates) {
    Json one = Json::makeObj();
    one.set("constraint_ids", constraintIdsToJson(p, c));
    one.set("size", Json::makeInt(static_cast<std::int64_t>(c.size())));
    cands.arr.push_back(std::move(one));
  }
  out.set("minimal_infeasible_candidates", cands);

  Json en = Json::makeObj();
  en.set("performed", Json::makeBool(d.enumerationPerformed));
  en.set("complete", Json::makeBool(d.enumerationComplete));
  en.set("subsets_tested", Json::makeInt(static_cast<std::int64_t>(d.subsetsTested)));
  if (!d.enumerationPerformed && !d.feasible)
    en.set("reason", Json::makeStr("constraint count exceeds enumeration limit " +
                                   std::to_string(kDiagnoseEnumMaxConstraints)));
  out.set("enumeration", en);

  std::cout << out.dump();
  return 0;
}

// --- selftest: randomized cross-check of solver vs naive reference ---

struct SelftestStats {
  int tested = 0;
  int feasible = 0;
  int infeasible = 0;
  int bruteForced = 0;
  int failures = 0;
};

// Validates that the reported cycle is structurally a cycle with negative
// total weight: consecutive constraints chain (head of one edge is the tail
// of the next) and the last edge returns to the first edge's tail.
bool validateCycle(const Problem& p, const SolveResult& r) {
  const auto& es = r.cycleConstraints;
  if (es.empty()) return false;
  std::int64_t w = 0;
  for (size_t i = 0; i < es.size(); ++i) {
    const Constraint& cur = p.constraints[es[i]];
    const Constraint& nxt = p.constraints[es[(i + 1) % es.size()]];
    if (cur.x != nxt.y) return false;  // head of cur must be tail of nxt
    w += cur.c;
  }
  return w < 0 && w == r.cycleWeight;
}

int cmdSelftest(int rounds, std::uint64_t seed) {
  std::mt19937_64 rng(seed);
  SelftestStats st;

  for (int t = 0; t < rounds; ++t) {
    // Small random system: n in [1,6], m in [0,10], c in [-5,5].
    int n = 1 + static_cast<int>(rng() % 6);
    int m = static_cast<int>(rng() % 11);
    Problem p;
    for (int i = 0; i < n; ++i) p.varNames.push_back("x" + std::to_string(i));
    for (int i = 0; i < m; ++i) {
      Constraint c;
      c.id = "c" + std::to_string(i);
      c.x = static_cast<int>(rng() % n);
      c.y = static_cast<int>(rng() % n);
      c.c = static_cast<std::int64_t>(rng() % 11) - 5;
      p.constraints.push_back(c);
    }

    SolveResult s = solve(p);
    ReferenceResult ref = referenceCheck(p, {});
    ++st.tested;
    if (ref.usedBruteForce) ++st.bruteForced;

    auto fail = [&](const char* what) {
      ++st.failures;
      std::fprintf(stderr, "selftest round %d: %s\n", t, what);
    };

    if (s.feasible != ref.feasible) {
      fail("solver/reference feasibility mismatch");
      continue;
    }
    if (s.feasible) {
      ++st.feasible;
      if (!findViolations(p, s.assignment).empty()) {
        fail("reported assignment violates constraints");
        continue;
      }
      // Normalization invariant: minimum value is exactly 0.
      std::int64_t mn = s.assignment[0];
      for (std::int64_t v : s.assignment) mn = std::min(mn, v);
      if (mn != 0) {
        fail("assignment not normalized (min != 0)");
        continue;
      }
    } else {
      ++st.infeasible;
      if (!validateCycle(p, s)) {
        fail("reported negative cycle is structurally invalid");
        continue;
      }
    }
  }

  Json out = Json::makeObj();
  out.set("seed", Json::makeInt(static_cast<std::int64_t>(seed)));
  out.set("tested", Json::makeInt(st.tested));
  out.set("feasible", Json::makeInt(st.feasible));
  out.set("infeasible", Json::makeInt(st.infeasible));
  out.set("reference_brute_forced", Json::makeInt(st.bruteForced));
  out.set("failures", Json::makeInt(st.failures));
  std::cout << out.dump();
  return st.failures == 0 ? 0 : 2;
}

void usage() {
  std::fprintf(stderr,
               "usage: diffc solve|verify|diagnose < request.json\n"
               "       diffc selftest [rounds] [seed]\n");
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    usage();
    return 1;
  }
  std::string mode = argv[1];

  try {
    if (mode == "selftest") {
      int rounds = argc > 2 ? std::stoi(argv[2]) : 2000;
      std::uint64_t seed = argc > 3 ? std::stoull(argv[3]) : 20260925ULL;
      return cmdSelftest(rounds, seed);
    }
    if (mode == "solve" || mode == "verify" || mode == "diagnose") {
      Json root = parseJson(readAllStdin());
      if (mode == "solve") return cmdSolve(root);
      if (mode == "verify") return cmdVerify(root);
      return cmdDiagnose(root);
    }
    usage();
    return 1;
  } catch (const JsonError& e) {
    Json out = Json::makeObj();
    out.set("error", Json::makeStr(std::string("invalid JSON: ") + e.message));
    std::cout << out.dump();
    return 1;
  } catch (const InputError& e) {
    Json out = Json::makeObj();
    out.set("error", Json::makeStr(e.message));
    std::cout << out.dump();
    return 1;
  }
}
