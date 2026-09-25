#include "api.hpp"

#include <chrono>

#include "dom.hpp"
#include "graph.hpp"
#include "naive.hpp"

namespace domtree {
namespace {

using domjson::JsonValue;
using J = JsonValue;

std::string jstr(const JsonValue* v, const std::string& def = "") {
  return (v && v->is_string()) ? v->str : def;
}

J error_response(const std::string& message) {
  J resp = J::make_object();
  resp.obj["success"] = J::make_bool(false);
  resp.obj["error"] = J::make_string(message);
  return resp;
}

J labels_array(const std::vector<int>& ids, const Graph& g) {
  J arr = J::make_array();
  for (int v : ids) arr.arr.push_back(J::make_string(g.name(v)));
  return arr;
}

J edge_pair_array(const std::vector<Edge>& edges, const Graph& g) {
  J arr = J::make_array();
  for (const Edge& e : edges) {
    J pair = J::make_array();
    pair.arr.push_back(J::make_string(g.name(e.from)));
    pair.arr.push_back(J::make_string(g.name(e.to)));
    arr.arr.push_back(pair);
  }
  return arr;
}

bool parse_nodes(const J& root, std::vector<std::string>& nodes,
                 std::string& err) {
  const J* v = root.find("nodes");
  if (!v || !v->is_array()) {
    err = "field \"nodes\" must be an array of node labels";
    return false;
  }
  for (const J& item : v->arr) {
    if (!item.is_string()) {
      err = "every entry in \"nodes\" must be a string";
      return false;
    }
    nodes.push_back(item.str);
  }
  return true;
}

bool parse_edges(const J& root,
                 std::vector<std::pair<std::string, std::string>>& edges,
                 std::string& err) {
  const J* v = root.find("edges");
  if (!v) return true;  // edges optional
  if (!v->is_array()) {
    err = "field \"edges\" must be an array";
    return false;
  }
  for (const J& item : v->arr) {
    if (item.is_array() && item.arr.size() == 2 &&
        item.arr[0].is_string() && item.arr[1].is_string()) {
      edges.emplace_back(item.arr[0].str, item.arr[1].str);
    } else if (item.is_object()) {
      const J* from = item.find("from");
      const J* to = item.find("to");
      if (!from || !to || !from->is_string() || !to->is_string()) {
        err = "edge object must have string fields \"from\" and \"to\"";
        return false;
      }
      edges.emplace_back(from->str, to->str);
    } else {
      err = "each edge must be [\"from\",\"to\"] or {\"from\":..,\"to\":..}";
      return false;
    }
  }
  return true;
}

J build_report(const Graph& g, int entry, const SolverResult& r,
               long long elapsed_us) {
  J report = J::make_object();
  report.obj["entry"] = J::make_string(g.name(entry));
  J stats = J::make_object();
  stats.obj["nodes_total"] = J::make_number(g.node_count());
  stats.obj["edges_total"] = J::make_number(g.edge_count());
  stats.obj["reachable_nodes"] = J::make_number(r.reachable_count);
  stats.obj["unreachable_nodes"] =
      J::make_number(g.node_count() - r.reachable_count);
  stats.obj["dataflow_iterations"] = J::make_number(r.iterations);
  stats.obj["elapsed_microseconds"] = J::make_number(
      static_cast<double>(elapsed_us));
  report.obj["statistics"] = stats;

  J reachable = J::make_array();
  J unreachable = J::make_array();
  for (int v = 0; v < g.node_count(); ++v) {
    if (r.reachable[v]) {
      reachable.arr.push_back(J::make_string(g.name(v)));
    } else {
      unreachable.arr.push_back(J::make_string(g.name(v)));
    }
  }
  report.obj["reachable"] = reachable;
  report.obj["unreachable"] = unreachable;

  // Immediate dominator tree: parent list and explicit edge list.
  J idom_list = J::make_array();
  J idom_edges = J::make_array();
  for (int v = 0; v < g.node_count(); ++v) {
    if (!r.reachable[v]) continue;
    J row = J::make_object();
    row.obj["node"] = J::make_string(g.name(v));
    if (r.idom[v] == -1) {
      row.obj["idom"] = J::make_null();  // entry
    } else {
      row.obj["idom"] = J::make_string(g.name(r.idom[v]));
      J pair = J::make_array();
      pair.arr.push_back(J::make_string(g.name(r.idom[v])));
      pair.arr.push_back(J::make_string(g.name(v)));
      idom_edges.arr.push_back(pair);
    }
    row.obj["tree_depth"] = J::make_number(r.tree_depth[v]);
    idom_list.arr.push_back(row);
  }
  report.obj["immediate_dominators"] = idom_list;
  report.obj["dominator_tree_edges"] = idom_edges;

  // Dominance frontiers for every reachable node (empty lists included,
  // so the output makes the empty/non-empty boundary explicit).
  J frontiers = J::make_object();
  for (int v = 0; v < g.node_count(); ++v) {
    if (!r.reachable[v]) continue;
    frontiers.obj[g.name(v)] = labels_array(r.frontier[v], g);
  }
  report.obj["dominance_frontiers"] = frontiers;

  // Full dominator sets, as independently readable evidence. Capped
  // because the dump is O(n^2): on large graphs use the "dominators"
  // query per node instead.
  constexpr int kDomSetDumpLimit = 200;
  if (g.node_count() <= kDomSetDumpLimit) {
    J domsets = J::make_object();
    for (int v = 0; v < g.node_count(); ++v) {
      if (!r.reachable[v]) continue;
      J members = J::make_array();
      for (int d = 0; d < g.node_count(); ++d) {
        if (r.dominates(d, v)) members.arr.push_back(J::make_string(g.name(d)));
      }
      domsets.obj[g.name(v)] = members;
    }
    report.obj["dominator_sets"] = domsets;
  } else {
    report.obj["dominator_sets_note"] = J::make_string(
        "omitted for graphs with more than " +
        std::to_string(kDomSetDumpLimit) +
        " nodes; use the \"dominators\" query per node");
  }

  J ev = J::make_object();
  ev.obj["back_edges"] = edge_pair_array(r.back_edges, g);
  ev.obj["edges_touching_unreachable"] =
      edge_pair_array(r.unreachable_edges, g);
  report.obj["evidence"] = ev;
  return report;
}

J query_error(const std::string& type, const std::string& node,
              const std::string& message) {
  J out = J::make_object();
  out.obj["type"] = J::make_string(type);
  if (!node.empty()) out.obj["node"] = J::make_string(node);
  out.obj["ok"] = J::make_bool(false);
  out.obj["error"] = J::make_string(message);
  return out;
}

J answer_query(const J& q, const Graph& g, const SolverResult& r) {
  std::string type = jstr(q.find("type"));
  if (type.empty()) return query_error("", "", "query missing \"type\"");

  auto resolve = [&](const char* field) -> int {
    const J* v = q.find(field);
    if (!v || !v->is_string()) return -2;  // -2: malformed
    return g.id(v->str);                   // -1: unknown label
  };

  if (type == "dominates" || type == "strictly_dominates") {
    int a = resolve("a");
    int b = resolve("b");
    J out = J::make_object();
    out.obj["type"] = J::make_string(type);
    if (a == -2 || b == -2) {
      out.obj["ok"] = J::make_bool(false);
      out.obj["error"] = J::make_string("query requires string fields a,b");
      return out;
    }
    const J* av = q.find("a");
    const J* bv = q.find("b");
    if (a == -1 || b == -1) {
      out.obj["ok"] = J::make_bool(false);
      out.obj["error"] = J::make_string("unknown node label");
      return out;
    }
    if (!r.reachable[a] || !r.reachable[b]) {
      out.obj["ok"] = J::make_bool(false);
      out.obj["error"] = J::make_string(
          "dominance is only defined for entry-reachable nodes");
      return out;
    }
    bool result = r.dominates(a, b);
    if (type == "strictly_dominates") result = result && (a != b);
    out.obj["ok"] = J::make_bool(true);
    out.obj["a"] = J::make_string(av->str);
    out.obj["b"] = J::make_string(bv->str);
    out.obj["result"] = J::make_bool(result);
    return out;
  }

  if (type == "idom" || type == "frontier" || type == "dominators" ||
      type == "dominated_by" || type == "dom_chain") {
    int v = resolve("node");
    const J* nv = q.find("node");
    J out = J::make_object();
    out.obj["type"] = J::make_string(type);
    if (v == -2) {
      out.obj["ok"] = J::make_bool(false);
      out.obj["error"] = J::make_string("query requires string field node");
      return out;
    }
    std::string label = nv ? nv->str : "";
    out.obj["node"] = J::make_string(label);
    if (v == -1) {
      out.obj["ok"] = J::make_bool(false);
      out.obj["error"] = J::make_string("unknown node label");
      return out;
    }
    if (!r.reachable[v]) {
      out.obj["ok"] = J::make_bool(false);
      out.obj["reachable"] = J::make_bool(false);
      out.obj["error"] = J::make_string(
          "node is unreachable from entry; dominators are undefined");
      return out;
    }
    out.obj["ok"] = J::make_bool(true);
    out.obj["reachable"] = J::make_bool(true);
    if (type == "idom") {
      if (r.idom[v] == -1) {
        out.obj["idom"] = J::make_null();
        out.obj["note"] = J::make_string("this is the entry node");
      } else {
        out.obj["idom"] = J::make_string(g.name(r.idom[v]));
      }
    } else if (type == "frontier") {
      out.obj["frontier"] = labels_array(r.frontier[v], g);
    } else if (type == "dominators") {
      std::vector<int> ds;
      for (int d = 0; d < g.node_count(); ++d)
        if (r.dominates(d, v)) ds.push_back(d);
      out.obj["dominators"] = labels_array(ds, g);
    } else if (type == "dominated_by") {
      out.obj["subtree"] = labels_array(dominated_subtree(r, v), g);
    } else {
      out.obj["chain"] = labels_array(dominator_chain(r, v), g);
    }
    return out;
  }

  return query_error(type, "", "unknown query type: " + type);
}

}  // namespace

std::string handle_request(const std::string& request_text) {
  J root;
  std::string err;
  if (!domjson::parse_json(request_text, root, err)) {
    return domjson::dump_json(error_response(err));
  }
  if (!root.is_object()) {
    return domjson::dump_json(
        error_response("request body must be a JSON object"));
  }

  std::string entry_label = jstr(root.find("entry"));
  if (entry_label.empty()) {
    return domjson::dump_json(
        error_response("field \"entry\" must name the entry node"));
  }
  std::vector<std::string> nodes;
  std::vector<std::pair<std::string, std::string>> edges;
  if (!parse_nodes(root, nodes, err) || !parse_edges(root, edges, err)) {
    return domjson::dump_json(error_response(err));
  }

  Graph g = Graph::build(nodes, edges, err);
  if (g.node_count() == 0 || !err.empty()) {
    if (err.empty()) err = "failed to build graph";
    return domjson::dump_json(error_response(err));
  }
  int entry = g.id(entry_label);
  if (entry == -1) {
    return domjson::dump_json(
        error_response("entry node not found in node list: " + entry_label));
  }

  SolverResult r;
  auto t0 = std::chrono::steady_clock::now();
  if (!solve(g, entry, r, err)) {
    return domjson::dump_json(error_response(err));
  }
  auto t1 = std::chrono::steady_clock::now();
  long long elapsed_us =
      std::chrono::duration_cast<std::chrono::microseconds>(t1 - t0).count();

  J resp = J::make_object();
  resp.obj["success"] = J::make_bool(true);
  resp.obj["result"] = build_report(g, entry, r, elapsed_us);

  const J* queries = root.find("queries");
  if (queries && queries->is_array()) {
    J qarr = J::make_array();
    for (const J& q : queries->arr) {
      if (!q.is_object()) {
        qarr.arr.push_back(query_error("", "", "each query must be an object"));
      } else {
        qarr.arr.push_back(answer_query(q, g, r));
      }
    }
    resp.obj["queries"] = qarr;
  }

  const J* verify = root.find("verify_naive");
  if (verify && verify->is_bool() && verify->boolean) {
    NaiveResult ref;
    std::string naive_err;
    J vrep = J::make_object();
    if (naive_solve(g, entry, ref, naive_err)) {
      std::vector<std::string> mm = compare_results(g, r, ref);
      vrep.obj["ran"] = J::make_bool(true);
      vrep.obj["paths_enumerated"] = J::make_number(
          static_cast<double>(ref.paths_enumerated));
      vrep.obj["match"] = J::make_bool(mm.empty());
      J mmarr = J::make_array();
      for (const std::string& m : mm) mmarr.arr.push_back(J::make_string(m));
      vrep.obj["mismatches"] = mmarr;
    } else {
      vrep.obj["ran"] = J::make_bool(false);
      vrep.obj["error"] = J::make_string(naive_err);
    }
    resp.obj["naive_verification"] = vrep;
  }

  return domjson::dump_json(resp);
}

}  // namespace domtree
