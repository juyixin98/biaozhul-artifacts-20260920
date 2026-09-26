#include <cstdio>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "json.h"
#include "solver.h"

namespace {

// Hard scale limits. The core algorithm itself is near-linear, but bounded
// input keeps memory and runtime predictable for a backend used by tests.
constexpr int kMaxVertices = 100000;
constexpr int kMaxOps = 200000;
constexpr int kMaxEdgeId = kMaxOps;

using dcsolve::OpInput;
using dcsolve::OpKind;
using dcsolve::SolveResult;
using dcjson::JsonValue;

std::string readAll(std::istream& in) {
  std::ostringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

JsonValue errorResponse(const std::string& code, const std::string& message, int opIndex) {
  JsonValue root = JsonValue::makeObject();
  root.set("ok", JsonValue::makeBool(false));
  JsonValue err = JsonValue::makeObject();
  err.set("code", JsonValue::makeString(code));
  err.set("message", JsonValue::makeString(message));
  if (opIndex >= 0) err.set("op_index", JsonValue::makeInt(opIndex));
  root.set("error", std::move(err));
  return root;
}

bool readIntField(const JsonValue& op, const char* key, int& out) {
  const JsonValue* v = op.find(key);
  if (!v || !v->isInt()) return false;
  long long x = v->intVal;
  if (x < -2147483648LL || x > 2147483647LL) return false;
  out = static_cast<int>(x);
  return true;
}

// Converts the request JSON into the internal op vector. Returns true on
// success; on failure `errCode`/`errMsg` describe the rejected request.
bool parseRequest(const JsonValue& root, int& n, bool& useNaive,
                  std::vector<OpInput>& ops, std::string& errCode, std::string& errMsg,
                  int& errOp) {
  errOp = -1;
  const JsonValue* jn = root.find("n");
  if (!jn || !jn->isInt() || jn->intVal < 1 || jn->intVal > kMaxVertices) {
    errCode = "invalid_n";
    errMsg = "'n' must be an integer in [1, " + std::to_string(kMaxVertices) + "]";
    return false;
  }
  n = static_cast<int>(jn->intVal);

  useNaive = false;
  if (const JsonValue* algo = root.find("algorithm")) {
    if (!algo->isString()) {
      errCode = "invalid_algorithm";
      errMsg = "'algorithm' must be \"segment-tree\" or \"naive-bfs\"";
      return false;
    }
    if (algo->str == "naive-bfs") {
      useNaive = true;
    } else if (algo->str != "segment-tree") {
      errCode = "invalid_algorithm";
      errMsg = "unknown algorithm '" + algo->str + "'";
      return false;
    }
  }

  const JsonValue* jops = root.find("operations");
  if (!jops || jops->type != dcjson::JsonType::Array) {
    errCode = "invalid_operations";
    errMsg = "'operations' must be an array";
    return false;
  }
  if (static_cast<int>(jops->arr.size()) > kMaxOps) {
    errCode = "too_many_operations";
    errMsg = "at most " + std::to_string(kMaxOps) + " operations are accepted";
    return false;
  }

  ops.reserve(jops->arr.size());
  int addCount = 0;
  for (size_t i = 0; i < jops->arr.size(); ++i) {
    const JsonValue& jop = jops->arr[i];
    if (jop.type != dcjson::JsonType::Object) {
      errCode = "invalid_operation";
      errMsg = "each operation must be an object";
      errOp = static_cast<int>(i);
      return false;
    }
    const JsonValue* jtype = jop.find("type");
    if (!jtype || !jtype->isString()) {
      errCode = "invalid_operation";
      errMsg = "operation is missing string field 'type'";
      errOp = static_cast<int>(i);
      return false;
    }
    OpInput op;
    if (jtype->str == "add") {
      op.kind = OpKind::Add;
      if (!readIntField(jop, "u", op.u) || !readIntField(jop, "v", op.v)) {
        errCode = "invalid_operation";
        errMsg = "add requires integer fields 'u' and 'v'";
        errOp = static_cast<int>(i);
        return false;
      }
      ++addCount;
      if (addCount > kMaxEdgeId) {
        errCode = "too_many_operations";
        errMsg = "too many add operations";
        errOp = static_cast<int>(i);
        return false;
      }
    } else if (jtype->str == "delete") {
      op.kind = OpKind::Delete;
      if (!readIntField(jop, "edge_id", op.edgeId) || op.edgeId < 0) {
        errCode = "invalid_operation";
        errMsg = "delete requires a non-negative integer 'edge_id' (returned by an earlier add)";
        errOp = static_cast<int>(i);
        return false;
      }
    } else if (jtype->str == "query") {
      op.kind = OpKind::Query;
      if (!readIntField(jop, "u", op.u) || !readIntField(jop, "v", op.v)) {
        errCode = "invalid_operation";
        errMsg = "query requires integer fields 'u' and 'v'";
        errOp = static_cast<int>(i);
        return false;
      }
      // Optional explicit time label; defaults to the operation index, which
      // is the "time just before/at op i" convention documented in README.
      if (const JsonValue* jt = jop.find("t")) {
        if (!jt->isInt() || jt->intVal < 0) {
          errCode = "invalid_operation";
          errMsg = "query field 't' must be a non-negative integer";
          errOp = static_cast<int>(i);
          return false;
        }
        op.t = static_cast<int>(jt->intVal);
      } else {
        op.t = static_cast<int>(i);
      }
    } else {
      errCode = "invalid_operation";
      errMsg = "unknown operation type '" + jtype->str + "'";
      errOp = static_cast<int>(i);
      return false;
    }
    ops.push_back(op);
  }
  return true;
}

const char* codeToString(dcsolve::SolveErrorCode code) {
  switch (code) {
    case dcsolve::SolveErrorCode::VertexOutOfRange: return "vertex_out_of_range";
    case dcsolve::SolveErrorCode::UnknownEdgeId: return "unknown_edge_id";
    case dcsolve::SolveErrorCode::DuplicateDelete: return "duplicate_delete";
    case dcsolve::SolveErrorCode::None: break;
  }
  return "solver_error";
}

JsonValue buildSuccess(bool useNaive, int n, const std::vector<OpInput>& ops,
                       const SolveResult& result) {
  JsonValue root = JsonValue::makeObject();
  root.set("ok", JsonValue::makeBool(true));
  root.set("algorithm",
           JsonValue::makeString(useNaive ? "naive-bfs" : "segment-tree"));
  root.set("n", JsonValue::makeInt(n));

  // edge_ids: instance id assigned to each add, in operation order.
  JsonValue edgeIds = JsonValue::makeArray();
  int nextId = 0;
  for (const OpInput& op : ops) {
    if (op.kind == OpKind::Add) edgeIds.arr.push_back(JsonValue::makeInt(nextId++));
  }
  root.set("edge_ids", std::move(edgeIds));

  JsonValue results = JsonValue::makeArray();
  for (const dcsolve::QueryEvent& q : result.queries) {
    JsonValue item = JsonValue::makeObject();
    item.set("op_index", JsonValue::makeInt(q.opIndex));
    item.set("t", JsonValue::makeInt(q.t));
    item.set("u", JsonValue::makeInt(q.u));
    item.set("v", JsonValue::makeInt(q.v));
    item.set("connected", JsonValue::makeBool(q.connected));
    results.arr.push_back(std::move(item));
  }
  root.set("results", std::move(results));

  JsonValue stats = JsonValue::makeObject();
  stats.set("num_vertices", JsonValue::makeInt(result.stats.numVertices));
  stats.set("num_operations", JsonValue::makeInt(result.stats.numOps));
  stats.set("num_adds", JsonValue::makeInt(result.stats.numAdds));
  stats.set("num_deletes", JsonValue::makeInt(result.stats.numDeletes));
  stats.set("num_queries", JsonValue::makeInt(result.stats.numQueries));
  stats.set("segment_placements",
            JsonValue::makeInt(static_cast<long long>(result.stats.segmentPlacements)));
  stats.set("union_calls",
            JsonValue::makeInt(static_cast<long long>(result.stats.unionCalls)));
  stats.set("merge_calls",
            JsonValue::makeInt(static_cast<long long>(result.stats.mergeCalls)));
  stats.set("max_rollback_stack",
            JsonValue::makeInt(static_cast<long long>(result.stats.maxRollbackDepth)));
  root.set("stats", std::move(stats));
  return root;
}

}  // namespace

int main(int argc, char** argv) {
  std::string input;
  if (argc >= 2) {
    std::ifstream file(argv[1]);
    if (!file) {
      std::cerr << "cannot open input file: " << argv[1] << "\n";
      return 1;
    }
    input = readAll(file);
  } else {
    input = readAll(std::cin);
  }

  dcjson::ParseResult parsed = dcjson::parse(input);
  if (!parsed.ok) {
    std::cout << errorResponse("invalid_json", "malformed JSON request: " + parsed.error, -1).dump(2)
              << "\n";
    return 1;
  }
  if (parsed.value.type != dcjson::JsonType::Object) {
    std::cout << errorResponse("invalid_request", "request body must be a JSON object", -1).dump(2)
              << "\n";
    return 1;
  }

  int n = 0;
  bool useNaive = false;
  std::vector<OpInput> ops;
  std::string errCode;
  std::string errMsg;
  int errOp = -1;
  if (!parseRequest(parsed.value, n, useNaive, ops, errCode, errMsg, errOp)) {
    std::cout << errorResponse(errCode, errMsg, errOp).dump(2) << "\n";
    return 2;
  }

  SolveResult result = useNaive
                           ? dcsolve::solveNaiveBFS(n, ops.data(), ops.size())
                           : dcsolve::solveSegmentTree(n, ops.data(), ops.size());
  if (!result.ok) {
    std::cout << errorResponse(codeToString(result.code), result.error, result.errorOpIndex).dump(2)
              << "\n";
    return 2;
  }

  std::cout << buildSuccess(useNaive, n, ops, result).dump(2) << "\n";
  return 0;
}
