// JSON interface for the small-scale SAT backend.
//
// Reads one JSON request from a file (argv[1]) or stdin, writes one JSON
// response to stdout. Exit code 0 on success, 2 on a bad request.
//
// Operations:
//   solve            {op, num_vars, clauses, proof?}        -> status, assignment?, proof?
//   reference        {op, num_vars, clauses}                -> exhaustive oracle (n <= 20)
//   check_assignment {op, num_vars, clauses, assignment}    -> valid
//   verify_proof     {op, num_vars, clauses, result, proof} -> valid
//   limits           {op}                                   -> the configured limits

#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "checker.hpp"
#include "cnf.hpp"
#include "dpll.hpp"
#include "json.hpp"
#include "reference.hpp"

namespace {

using minijson::Value;

Value error_response(const std::string& message) {
  Value resp = Value::object();
  resp.object_value["status"] = Value::string("error");
  resp.object_value["error"] = Value::string(message);
  return resp;
}

sat::Formula parse_formula(const Value& req) {
  sat::Formula f;
  f.num_vars = static_cast<int>(req.at("num_vars").as_int());
  for (const Value& cv : req.at("clauses").as_array()) {
    sat::Clause c;
    for (const Value& lv : cv.as_array()) {
      long long lit = lv.as_int();
      c.literals.push_back(static_cast<sat::Literal>(lit));
    }
    f.clauses.push_back(std::move(c));
  }
  return sat::normalize_formula(f);
}

std::vector<int8_t> parse_assignment(const Value& v, int num_vars) {
  const std::vector<Value>& arr = v.as_array();
  if (arr.size() != static_cast<size_t>(num_vars)) {
    throw minijson::Error("assignment length must equal num_vars");
  }
  std::vector<int8_t> assignment(static_cast<size_t>(num_vars) + 1, -1);
  for (size_t i = 0; i < arr.size(); ++i) {
    if (arr[i].is_null()) continue;  // don't care
    assignment[i + 1] = arr[i].as_bool() ? 1 : 0;
  }
  return assignment;
}

sat::Event parse_event(const Value& v) {
  sat::Event e;
  const std::string& kind = v.at("ev").as_string();
  if (kind == "decide") {
    e.kind = sat::Event::Kind::kDecide;
    e.var = static_cast<int>(v.at("var").as_int());
    e.value = v.at("value").as_bool();
  } else if (kind == "unit") {
    e.kind = sat::Event::Kind::kUnit;
    e.lit = static_cast<sat::Literal>(v.at("lit").as_int());
    e.clause = static_cast<int>(v.at("clause").as_int());
  } else if (kind == "conflict") {
    e.kind = sat::Event::Kind::kConflict;
    e.clause = static_cast<int>(v.at("clause").as_int());
  } else if (kind == "backtrack") {
    e.kind = sat::Event::Kind::kBacktrack;
    e.var = static_cast<int>(v.at("var").as_int());
  } else {
    throw minijson::Error("unknown event kind: " + kind);
  }
  return e;
}

Value event_to_json(const sat::Event& e) {
  Value v = Value::object();
  switch (e.kind) {
    case sat::Event::Kind::kDecide:
      v.object_value["ev"] = Value::string("decide");
      v.object_value["var"] = Value::integer(e.var);
      v.object_value["value"] = Value::boolean(e.value);
      break;
    case sat::Event::Kind::kUnit:
      v.object_value["ev"] = Value::string("unit");
      v.object_value["lit"] = Value::integer(e.lit);
      v.object_value["clause"] = Value::integer(e.clause);
      break;
    case sat::Event::Kind::kConflict:
      v.object_value["ev"] = Value::string("conflict");
      v.object_value["clause"] = Value::integer(e.clause);
      break;
    case sat::Event::Kind::kBacktrack:
      v.object_value["ev"] = Value::string("backtrack");
      v.object_value["var"] = Value::integer(e.var);
      break;
  }
  return v;
}

Value assignment_to_json(const std::vector<int8_t>& assignment) {
  Value arr = Value::array();
  for (size_t i = 1; i < assignment.size(); ++i) {
    if (assignment[i] < 0) {
      arr.array_value.push_back(Value::null());
    } else {
      arr.array_value.push_back(Value::boolean(assignment[i] == 1));
    }
  }
  return arr;
}

Value valid_response(bool ok, const std::string& error) {
  Value resp = Value::object();
  resp.object_value["status"] = Value::string("ok");
  resp.object_value["valid"] = Value::boolean(ok);
  if (!ok) resp.object_value["error"] = Value::string(error);
  return resp;
}

Value handle_solve(const Value& req) {
  sat::Formula f = parse_formula(req);
  bool want_proof = true;
  if (req.has("proof")) want_proof = req.at("proof").as_bool();

  sat::SolveResult result;
  try {
    result = sat::dpll_solve(f);
  } catch (const sat::LimitExceeded& e) {
    Value resp = Value::object();
    resp.object_value["status"] = Value::string("unknown");
    resp.object_value["error"] = Value::string(e.what());
    return resp;
  }

  Value resp = Value::object();
  resp.object_value["status"] =
      Value::string(result.status == sat::Status::kSat ? "sat" : "unsat");
  if (result.status == sat::Status::kSat) {
    resp.object_value["assignment"] = assignment_to_json(result.assignment);
  }
  if (want_proof) {
    Value events = Value::array();
    for (const sat::Event& e : result.events) {
      events.array_value.push_back(event_to_json(e));
    }
    resp.object_value["proof"] = std::move(events);
  }
  return resp;
}

Value handle_reference(const Value& req) {
  sat::Formula f = parse_formula(req);
  sat::ReferenceResult result = sat::reference_solve(f);
  Value resp = Value::object();
  resp.object_value["status"] =
      Value::string(result.status == sat::Status::kSat ? "sat" : "unsat");
  resp.object_value["num_satisfying"] =
      Value::integer(static_cast<long long>(result.num_satisfying));
  if (result.status == sat::Status::kSat) {
    resp.object_value["assignment"] = assignment_to_json(result.assignment);
  }
  return resp;
}

Value handle_check_assignment(const Value& req) {
  sat::Formula f = parse_formula(req);
  std::vector<int8_t> assignment =
      parse_assignment(req.at("assignment"), f.num_vars);
  sat::CheckResult check = sat::check_assignment(f, assignment);
  return valid_response(check.ok, check.error);
}

Value handle_verify_proof(const Value& req) {
  sat::Formula f = parse_formula(req);
  const std::string& result_str = req.at("result").as_string();
  sat::Status claimed;
  if (result_str == "sat") {
    claimed = sat::Status::kSat;
  } else if (result_str == "unsat") {
    claimed = sat::Status::kUnsat;
  } else {
    throw minijson::Error("result must be \"sat\" or \"unsat\"");
  }
  const std::vector<Value>& proof = req.at("proof").as_array();
  constexpr size_t kMaxProofEvents = 5000000;
  if (proof.size() > kMaxProofEvents) {
    throw minijson::Error("proof too large");
  }
  std::vector<sat::Event> events;
  events.reserve(proof.size());
  for (const Value& v : proof) events.push_back(parse_event(v));
  sat::CheckResult check = sat::verify_proof(f, events, claimed);
  return valid_response(check.ok, check.error);
}

Value handle_limits() {
  Value resp = Value::object();
  resp.object_value["status"] = Value::string("ok");
  resp.object_value["max_vars"] = Value::integer(sat::kMaxVars);
  resp.object_value["max_clauses"] = Value::integer(sat::kMaxClauses);
  resp.object_value["max_clause_length"] = Value::integer(sat::kMaxClauseLength);
  resp.object_value["max_reference_vars"] = Value::integer(sat::kMaxReferenceVars);
  return resp;
}

Value handle(const Value& req) {
  const std::string& op = req.at("op").as_string();
  if (op == "solve") return handle_solve(req);
  if (op == "reference") return handle_reference(req);
  if (op == "check_assignment") return handle_check_assignment(req);
  if (op == "verify_proof") return handle_verify_proof(req);
  if (op == "limits") return handle_limits();
  throw minijson::Error("unknown op: " + op);
}

}  // namespace

int main(int argc, char** argv) {
  if (argc > 2) {
    std::cerr << "usage: sat_backend [request.json]  (reads stdin without args)\n";
    return 2;
  }
  std::string text;
  if (argc == 2) {
    std::ifstream in(argv[1]);
    if (!in) {
      std::cerr << "cannot open " << argv[1] << "\n";
      return 2;
    }
    std::ostringstream ss;
    ss << in.rdbuf();
    text = ss.str();
  } else {
    std::ostringstream ss;
    ss << std::cin.rdbuf();
    text = ss.str();
  }

  try {
    Value req = minijson::Parser(text).parse();
    std::cout << minijson::serialize_pretty(handle(req)) << "\n";
    return 0;
  } catch (const std::exception& e) {
    std::cout << minijson::serialize_pretty(error_response(e.what())) << "\n";
    return 2;
  }
}
