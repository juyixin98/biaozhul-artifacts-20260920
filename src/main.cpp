// Command line interface (pure backend, JSON over stdin/files).
//
//   mincut solve  [request.json]   -> full max-flow/min-cut evidence
//   mincut brute  [request.json]   -> exhaustive reference min cut
//   mincut verify [response.json]  -> independently verify a solve response
//
// With no file argument the document is read from standard input.
#include <algorithm>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <vector>

#include "brute.h"
#include "dinic.h"
#include "minjson.h"
#include "network.h"
#include "verifier.h"

namespace {

using namespace mincut;
using namespace minjson;

constexpr int kMaxNodes = 200;
constexpr int kMaxEdges = 1000;
constexpr int kMaxBruteNodes = 20;
constexpr long long kMaxCapacity = 1'000'000'000'000LL;  // 10^12

struct RequestError {
  std::string message;
};

std::string readAll(std::istream& in) {
  std::ostringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

std::string readInput(const std::string& path) {
  if (path == "-" || path.empty()) return readAll(std::cin);
  std::ifstream f(path);
  if (!f) throw RequestError{"cannot open input file: " + path};
  return readAll(f);
}

Value errorResponse(const std::string& message) {
  Value v = Value::makeObject();
  v.set("ok", Value::makeBool(false));
  v.set("error", Value::makeString(message));
  return v;
}

void requireArray(const Value* v, const char* name) {
  if (!v || v->type != Type::Array) throw RequestError{std::string("missing or invalid field: ") + name};
}

long long requireNonNegativeInt(const Value& v, const char* what) {
  if (v.type != Type::Integer) throw RequestError{std::string(what) + " must be an integer"};
  if (v.integer < 0) throw RequestError{std::string(what) + " must be non-negative"};
  return v.integer;
}

// Parses and validates a request; echoes it back with defaults (edge ids)
// filled in.
struct ParsedRequest {
  Network net;
  Value echo;  // normalized request object
};

ParsedRequest parseRequest(const Value& req) {
  if (req.type != Type::Object) throw RequestError{"request must be a JSON object"};
  const Value* nodes = req.find("nodes");
  const Value* edges = req.find("edges");
  const Value* source = req.find("source");
  const Value* sink = req.find("sink");
  requireArray(nodes, "nodes");
  requireArray(edges, "edges");
  if (!source || source->type != Type::String) throw RequestError{"missing string field: source"};
  if (!sink || sink->type != Type::String) throw RequestError{"missing string field: sink"};

  ParsedRequest out;
  out.echo = Value::makeObject();
  std::set<std::string> names;
  for (const Value& nv : nodes->items) {
    if (nv.type != Type::String) throw RequestError{"every node must be a string"};
    if (!names.insert(nv.text).second) throw RequestError{"duplicate node: " + nv.text};
    out.net.node_names.push_back(nv.text);
  }
  int n = static_cast<int>(out.net.node_names.size());
  if (n < 2) throw RequestError{"network needs at least 2 nodes"};
  if (n > kMaxNodes) throw RequestError{"too many nodes (limit " + std::to_string(kMaxNodes) + ")"};
  if (static_cast<int>(edges->items.size()) > kMaxEdges)
    throw RequestError{"too many edges (limit " + std::to_string(kMaxEdges) + ")"};

  std::map<std::string, int> idx;
  for (int i = 0; i < n; ++i) idx[out.net.node_names[i]] = i;
  if (!idx.count(source->text)) throw RequestError{"source not in nodes: " + source->text};
  if (!idx.count(sink->text)) throw RequestError{"sink not in nodes: " + sink->text};
  if (source->text == sink->text) throw RequestError{"source and sink must differ"};
  out.net.source = idx[source->text];
  out.net.sink = idx[sink->text];

  std::set<std::string> edge_ids;
  Value echo_edges = Value::makeArray();
  for (std::size_t i = 0; i < edges->items.size(); ++i) {
    const Value& ev = edges->items[i];
    if (ev.type != Type::Object) throw RequestError{"every edge must be an object"};
    const Value* from = ev.find("from");
    const Value* to = ev.find("to");
    const Value* cap = ev.find("capacity");
    if (!from || from->type != Type::String) throw RequestError{"edge missing string 'from'"};
    if (!to || to->type != Type::String) throw RequestError{"edge missing string 'to'"};
    if (!cap) throw RequestError{"edge missing 'capacity'"};
    long long c = requireNonNegativeInt(*cap, "capacity");
    if (c > kMaxCapacity)
      throw RequestError{"capacity exceeds limit " + std::to_string(kMaxCapacity)};
    if (!idx.count(from->text)) throw RequestError{"edge endpoint unknown: " + from->text};
    if (!idx.count(to->text)) throw RequestError{"edge endpoint unknown: " + to->text};

    InputEdge e;
    const Value* idv = ev.find("id");
    if (idv) {
      if (idv->type != Type::String) throw RequestError{"edge id must be a string"};
      e.id = idv->text;
    } else {
      e.id = "e" + std::to_string(i);
    }
    if (!edge_ids.insert(e.id).second) throw RequestError{"duplicate edge id: " + e.id};
    e.from = idx[from->text];
    e.to = idx[to->text];
    e.capacity = c;
    out.net.edges.push_back(e);

    Value ee = Value::makeObject();
    ee.set("id", Value::makeString(e.id));
    ee.set("from", Value::makeString(from->text));
    ee.set("to", Value::makeString(to->text));
    ee.set("capacity", Value::makeInt(c));
    echo_edges.push(std::move(ee));
  }

  Value echo_nodes = Value::makeArray();
  for (const std::string& name : out.net.node_names)
    echo_nodes.push(Value::makeString(name));
  out.echo.set("nodes", std::move(echo_nodes));
  out.echo.set("edges", std::move(echo_edges));
  out.echo.set("source", Value::makeString(source->text));
  out.echo.set("sink", Value::makeString(sink->text));
  return out;
}

Value buildResidualEvidence(const Network& net,
                            const std::vector<std::vector<ResidualArc>>& residual) {
  Value nodes = Value::makeArray();
  for (int u = 0; u < static_cast<int>(net.node_names.size()); ++u) {
    Value rec = Value::makeObject();
    rec.set("node", Value::makeString(net.node_names[u]));
    Value arcs = Value::makeArray();
    for (const ResidualArc& a : residual[u]) {
      Value av = Value::makeObject();
      av.set("to", Value::makeString(net.node_names[a.to]));
      av.set("edge_id", Value::makeString(net.edges[a.edge_id].id));
      // Every arc is tagged: forward arc of its own input edge, or the
      // artificial residual reverse arc created for that edge. Input edges
      // in the opposite direction appear as separate 'forward' arcs.
      av.set("kind", Value::makeString(a.artificial ? "artificial_reverse" : "forward"));
      av.set("residual_capacity", Value::makeInt(a.cap));
      arcs.push(std::move(av));
    }
    rec.set("arcs", std::move(arcs));
    nodes.push(std::move(rec));
  }
  Value out = Value::makeObject();
  out.set("nodes", std::move(nodes));
  return out;
}

int solve(const std::string& path) {
  std::string raw;
  try {
    raw = readInput(path);
  } catch (const RequestError& e) {
    std::cout << dump(errorResponse(e.message)) << '\n';
    return 2;
  }
  ParseError perr;
  auto parsed = minjson::parse(raw, &perr);
  if (!parsed) {
    std::cout << dump(errorResponse("invalid JSON at offset " +
                                    std::to_string(perr.offset) + ": " +
                                    perr.message))
              << '\n';
    return 2;
  }

  ParsedRequest req;
  try {
    req = parseRequest(*parsed);
  } catch (const RequestError& e) {
    std::cout << dump(errorResponse(e.message)) << '\n';
    return 2;
  }

  Dinic dinic(req.net);
  SolveResult result = dinic.run();

  const int n = static_cast<int>(req.net.node_names.size());

  // Flows from residual capacities: f(e) = c(e) - residual forward cap.
  Value flows = Value::makeArray();
  for (int i = 0; i < static_cast<int>(req.net.edges.size()); ++i) {
    const ArcLocation& loc = result.forward_arc[i];
    const ResidualArc& arc = result.residual[loc.node][loc.index];
    long long flow = req.net.edges[i].capacity - arc.cap;
    Value fv = Value::makeObject();
    fv.set("edge_id", Value::makeString(req.net.edges[i].id));
    fv.set("flow", Value::makeInt(flow));
    flows.push(std::move(fv));
  }

  Value source_side = Value::makeArray();
  Value sink_side = Value::makeArray();
  Value cut_edges = Value::makeArray();
  long long cut_value = 0;
  for (int v = 0; v < n; ++v) {
    Value name = Value::makeString(req.net.node_names[v]);
    if (result.source_reachable[v]) source_side.push(std::move(name));
    else sink_side.push(Value::makeString(req.net.node_names[v]));
  }
  for (const InputEdge& e : req.net.edges) {
    if (result.source_reachable[e.from] && !result.source_reachable[e.to]) {
      cut_edges.push(Value::makeString(e.id));
      cut_value += e.capacity;
    }
  }

  Value cut = Value::makeObject();
  cut.set("source_side", std::move(source_side));
  cut.set("sink_side", std::move(sink_side));
  cut.set("cut_edges", std::move(cut_edges));
  cut.set("cut_value", Value::makeInt(cut_value));

  Value stats = Value::makeObject();
  stats.set("bfs_rounds", Value::makeInt(result.stats.bfs_rounds));
  stats.set("augmentations", Value::makeInt(result.stats.augmentations));

  Value resp = Value::makeObject();
  resp.set("ok", Value::makeBool(true));
  resp.set("request", std::move(req.echo));
  resp.set("max_flow", Value::makeInt(result.max_flow));
  resp.set("flows", std::move(flows));
  resp.set("cut", std::move(cut));
  resp.set("residual", buildResidualEvidence(req.net, result.residual));
  resp.set("stats", std::move(stats));
  std::cout << dump(resp) << '\n';
  return 0;
}

int brute(const std::string& path) {
  std::string raw = readInput(path);
  ParseError perr;
  auto parsed = minjson::parse(raw, &perr);
  if (!parsed) {
    std::cout << dump(errorResponse("invalid JSON: " + perr.message)) << '\n';
    return 2;
  }
  ParsedRequest req;
  try {
    req = parseRequest(*parsed);
  } catch (const RequestError& e) {
    std::cout << dump(errorResponse(e.message)) << '\n';
    return 2;
  }
  if (static_cast<int>(req.net.node_names.size()) > kMaxBruteNodes) {
    std::cout << dump(errorResponse(
                     "brute force supports at most " +
                     std::to_string(kMaxBruteNodes) + " nodes"))
              << '\n';
    return 2;
  }
  BruteResult br = bruteForceMinCut(req.net);
  Value sside = Value::makeArray();
  Value tside = Value::makeArray();
  for (int v = 0; v < static_cast<int>(req.net.node_names.size()); ++v) {
    if (br.source_side[v]) sside.push(Value::makeString(req.net.node_names[v]));
    else tside.push(Value::makeString(req.net.node_names[v]));
  }
  Value out = Value::makeObject();
  out.set("ok", Value::makeBool(true));
  out.set("min_cut_value", Value::makeInt(br.min_cut_value));
  out.set("source_side", std::move(sside));
  out.set("sink_side", std::move(tside));
  out.set("partitions_checked", Value::makeInt(br.partitions_checked));
  std::cout << dump(out) << '\n';
  return 0;
}

int verify(const std::string& path) {
  std::string raw;
  try {
    raw = readInput(path);
  } catch (const RequestError& e) {
    std::cout << dump(errorResponse(e.message)) << '\n';
    return 2;
  }
  ParseError perr;
  auto parsed = minjson::parse(raw, &perr);
  if (!parsed) {
    std::cout << dump(errorResponse("invalid JSON: " + perr.message)) << '\n';
    return 2;
  }
  VerificationReport report = verifyResponse(*parsed);
  std::cout << dump(reportToJson(report)) << '\n';
  return report.valid ? 0 : 1;
}

void usage() {
  std::cerr << "usage: mincut <solve|brute|verify> [file.json|-]\n";
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    usage();
    return 64;
  }
  std::string mode = argv[1];
  std::string path = argc >= 3 ? argv[2] : "-";
  try {
    if (mode == "solve") return solve(path);
    if (mode == "brute") return brute(path);
    if (mode == "verify") return verify(path);
  } catch (const std::exception& e) {
    std::cout << dump(errorResponse(std::string("internal error: ") + e.what())) << '\n';
    return 70;
  }
  usage();
  return 64;
}
