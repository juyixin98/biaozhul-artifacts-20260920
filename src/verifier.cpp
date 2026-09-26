#include "verifier.h"

#include <algorithm>
#include <map>
#include <queue>
#include <set>
#include <sstream>

namespace mincut {

namespace {

const minjson::Value* field(const minjson::Value& obj, const std::string& key) {
  return obj.find(key);
}

bool asInt(const minjson::Value& v, long long* out) {
  if (v.type != minjson::Type::Integer) return false;
  *out = v.integer;
  return true;
}

bool asString(const minjson::Value& v, std::string* out) {
  if (v.type != minjson::Type::String) return false;
  *out = v.text;
  return true;
}

struct EEdge {
  std::string id;
  std::string from;
  std::string to;
  long long capacity;
};

}  // namespace

VerificationReport verifyResponse(const minjson::Value& root) {
  VerificationReport rep;
  rep.valid = true;

  const minjson::Value* ok = field(root, "ok");
  if (ok && ok->type == minjson::Type::Boolean && !ok->boolean) {
    rep.fail("response is an error response (ok = false)");
    return rep;
  }

  const minjson::Value* req = field(root, "request");
  const minjson::Value* nodes_json = req ? field(*req, "nodes") : nullptr;
  const minjson::Value* edges_json = req ? field(*req, "edges") : nullptr;
  const minjson::Value* src_json = req ? field(*req, "source") : nullptr;
  const minjson::Value* snk_json = req ? field(*req, "sink") : nullptr;
  if (!nodes_json || !edges_json || !src_json || !snk_json) {
    rep.fail("missing request/nodes/edges/source/sink echo in response");
    return rep;
  }

  // ---- Rebuild the graph ------------------------------------------------
  std::vector<std::string> nodes;
  std::set<std::string> node_set;
  for (const minjson::Value& nv : nodes_json->items) {
    std::string name;
    if (!asString(nv, &name)) {
      rep.fail("node name is not a string");
      return rep;
    }
    if (!node_set.insert(name).second) {
      rep.fail("duplicate node name: " + name);
      return rep;
    }
    nodes.push_back(name);
  }
  std::map<std::string, int> index;
  for (std::size_t i = 0; i < nodes.size(); ++i) index[nodes[i]] = static_cast<int>(i);

  std::string source_name, sink_name;
  asString(*src_json, &source_name);
  asString(*snk_json, &sink_name);
  if (!index.count(source_name)) { rep.fail("source not among nodes"); return rep; }
  if (!index.count(sink_name)) { rep.fail("sink not among nodes"); return rep; }
  if (source_name == sink_name) { rep.fail("source equals sink"); return rep; }

  std::vector<EEdge> edges;
  std::set<std::string> edge_ids;
  for (const minjson::Value& ev : edges_json->items) {
    EEdge e;
    const minjson::Value* id = field(ev, "id");
    const minjson::Value* fr = field(ev, "from");
    const minjson::Value* to = field(ev, "to");
    const minjson::Value* cap = field(ev, "capacity");
    if (!id || !fr || !to || !cap ||
        !asString(*id, &e.id) || !asString(*fr, &e.from) ||
        !asString(*to, &e.to) || !asInt(*cap, &e.capacity)) {
      rep.fail("malformed edge record");
      return rep;
    }
    if (!edge_ids.insert(e.id).second) {
      rep.fail("duplicate edge id: " + e.id);
      return rep;
    }
    if (e.capacity < 0) {
      rep.fail("negative capacity on edge " + e.id);
      return rep;
    }
    if (!index.count(e.from) || !index.count(e.to)) {
      rep.fail("edge endpoint not declared in nodes: " + e.id);
      return rep;
    }
    edges.push_back(e);
  }

  // ---- Flows ------------------------------------------------------------
  const minjson::Value* flows = field(root, "flows");
  if (!flows || flows->type != minjson::Type::Array) {
    rep.fail("missing flows array");
    return rep;
  }
  std::map<std::string, long long> flow_value;
  std::set<std::string> flow_seen;
  for (const minjson::Value& fv : flows->items) {
    std::string eid;
    long long f;
    const minjson::Value* eidv = field(fv, "edge_id");
    const minjson::Value* fval = field(fv, "flow");
    if (!eidv || !fval || !asString(*eidv, &eid) || !asInt(*fval, &f)) {
      rep.fail("malformed flow record");
      return rep;
    }
    if (!flow_seen.insert(eid).second) {
      rep.fail("duplicate flow record for edge " + eid);
      return rep;
    }
    flow_value[eid] = f;
  }

  // C1 capacity bounds.
  for (const EEdge& e : edges) {
    auto it = flow_value.find(e.id);
    if (it == flow_value.end()) {
      rep.fail("no flow record for edge " + e.id);
      continue;
    }
    long long f = it->second;
    if (f < 0) rep.fail("negative flow on edge " + e.id);
    if (f > e.capacity)
      rep.fail("flow " + std::to_string(f) + " exceeds capacity " +
               std::to_string(e.capacity) + " on edge " + e.id);
  }
  for (const auto& [eid, f] : flow_value) {
    if (!edge_ids.count(eid)) rep.fail("flow for unknown edge " + eid);
  }
  if (!rep.valid) return rep;

  // C2/C3 conservation and terminal balances.
  const int n = static_cast<int>(nodes.size());
  std::vector<long long> balance(n, 0);
  for (const EEdge& e : edges) {
    long long f = flow_value[e.id];
    balance[index[e.from]] += f;
    balance[index[e.to]] -= f;
  }
  int s = index[source_name];
  int t = index[sink_name];
  rep.source_outflow = balance[s];
  rep.sink_inflow = -balance[t];
  for (int v = 0; v < n; ++v) {
    if (v == s || v == t) continue;
    if (balance[v] != 0)
      rep.fail("flow conservation violated at node " + nodes[v] +
               " (net outflow " + std::to_string(balance[v]) + ")");
  }
  if (rep.source_outflow < 0) rep.fail("source has net inflow, not outflow");
  if (rep.sink_inflow < 0) rep.fail("sink has net outflow, not inflow");
  if (rep.source_outflow != rep.sink_inflow)
    rep.fail("source outflow " + std::to_string(rep.source_outflow) +
             " != sink inflow " + std::to_string(rep.sink_inflow));

  long long reported_flow = -1;
  const minjson::Value* mf = field(root, "max_flow");
  if (!mf || !asInt(*mf, &reported_flow) || reported_flow < 0) {
    rep.fail("missing or invalid max_flow");
  } else if (reported_flow != rep.source_outflow) {
    rep.fail("reported max_flow " + std::to_string(reported_flow) +
             " != flow value derived from flows " +
             std::to_string(rep.source_outflow));
  }

  // ---- Partition --------------------------------------------------------
  const minjson::Value* cut = field(root, "cut");
  const minjson::Value* sside = cut ? field(*cut, "source_side") : nullptr;
  const minjson::Value* tside = cut ? field(*cut, "sink_side") : nullptr;
  const minjson::Value* cutedges = cut ? field(*cut, "cut_edges") : nullptr;
  const minjson::Value* cutval = cut ? field(*cut, "cut_value") : nullptr;
  if (!sside || !tside || !cutedges || !cutval) {
    rep.fail("missing cut/source_side/sink_side/cut_edges/cut_value");
    return rep;
  }
  std::vector<char> in_s(n, 0), in_t(n, 0);
  for (const minjson::Value& sv : sside->items) {
    std::string name;
    if (!asString(sv, &name) || !index.count(name)) {
      rep.fail("unknown node in source_side: " + name);
      continue;
    }
    in_s[index[name]] = 1;
  }
  for (const minjson::Value& tv : tside->items) {
    std::string name;
    if (!asString(tv, &name) || !index.count(name)) {
      rep.fail("unknown node in sink_side: " + name);
      continue;
    }
    in_t[index[name]] = 1;
  }
  // C4 exact partition.
  if (!in_s[s]) rep.fail("source is not in source side");
  if (!in_t[t]) rep.fail("sink is not in sink side");
  for (int v = 0; v < n; ++v) {
    if (in_s[v] && in_t[v])
      rep.fail("node " + nodes[v] + " appears on both sides");
    if (!in_s[v] && !in_t[v])
      rep.fail("node " + nodes[v] + " appears on neither side");
  }

  // C5 cut set is exactly S -> T edges; C6 cut value.
  std::set<std::string> expected_cut;
  long long computed_cut = 0;
  for (const EEdge& e : edges) {
    if (in_s[index[e.from]] && in_t[index[e.to]]) {
      expected_cut.insert(e.id);
      computed_cut += e.capacity;
    }
  }
  std::set<std::string> reported_cut;
  for (const minjson::Value& cv : cutedges->items) {
    std::string eid;
    if (!asString(cv, &eid)) {
      rep.fail("cut edge id is not a string");
      continue;
    }
    if (!edge_ids.count(eid)) rep.fail("cut lists unknown edge " + eid);
    if (!reported_cut.insert(eid).second)
      rep.fail("cut edge listed twice: " + eid);
  }
  if (reported_cut != expected_cut) {
    for (const std::string& eid : expected_cut)
      if (!reported_cut.count(eid)) rep.fail("cut set omits crossing edge " + eid);
    for (const std::string& eid : reported_cut)
      if (!expected_cut.count(eid))
        rep.fail("cut set includes non-crossing edge " + eid);
  }
  rep.cut_value_computed = computed_cut;
  long long reported_cut_value = -1;
  if (!asInt(*cutval, &reported_cut_value)) {
    rep.fail("cut_value is not an integer");
  } else if (reported_cut_value != computed_cut) {
    rep.fail("reported cut_value " + std::to_string(reported_cut_value) +
             " != capacity sum of cut edges " + std::to_string(computed_cut));
  }

  // C7 strong duality.
  if (reported_flow >= 0 && reported_cut_value >= 0 &&
      reported_flow != reported_cut_value) {
    rep.fail("max flow " + std::to_string(reported_flow) +
             " != cut value " + std::to_string(reported_cut_value));
  }

  // C8 independent residual reachability: no residual arc leaves S.
  //    Recompute residual capacities purely from per-edge flows.
  std::vector<std::vector<int>> radj_fwd(n);   // residual arcs u->v
  std::vector<std::vector<int>> radj_rev(n);   // residual arcs v->u
  for (std::size_t i = 0; i < edges.size(); ++i) {
    int u = index[edges[i].from];
    int v = index[edges[i].to];
    long long f = flow_value[edges[i].id];
    if (f < edges[i].capacity) { radj_fwd[u].push_back(v); }
    if (f > 0) { radj_rev[v].push_back(u); }
  }
  std::vector<char> reached(n, 0);
  std::queue<int> q;
  reached[s] = 1;
  q.push(s);
  while (!q.empty()) {
    int u = q.front();
    q.pop();
    for (int w : radj_fwd[u])
      if (!reached[w]) { reached[w] = 1; q.push(w); }
    for (int w : radj_rev[u])
      if (!reached[w]) { reached[w] = 1; q.push(w); }
  }
  rep.residual_reachable_count = 0;
  for (char r : reached) rep.residual_reachable_count += r;
  for (int v = 0; v < n; ++v) {
    if (reached[v] != in_s[v]) {
      rep.fail(std::string("residual reachability disagrees with partition at ") +
               nodes[v] + " (reached=" + (reached[v] ? "S" : "T") +
               ", reported=" + (in_s[v] ? "S" : "T") + ")");
    }
  }

  // C9 residual listing: exactly one own forward arc and one own artificial
  //    reverse arc per input edge, with correct residual capacities.
  const minjson::Value* residual = field(root, "residual");
  const minjson::Value* rnodes = residual ? field(*residual, "nodes") : nullptr;
  if (!rnodes) {
    rep.fail("missing residual.nodes evidence");
  } else {
    std::map<std::string, int> forward_seen;   // edge_id -> count
    std::map<std::string, int> reverse_seen;
    for (const minjson::Value& rv : rnodes->items) {
      const minjson::Value* nodev = field(rv, "node");
      const minjson::Value* arcs = field(rv, "arcs");
      std::string owner;
      if (!nodev || !asString(*nodev, &owner) || !arcs) {
        rep.fail("malformed residual node record");
        continue;
      }
      auto owner_it = index.find(owner);
      if (owner_it == index.end()) {
        rep.fail("residual arc owned by unknown node " + owner);
        continue;
      }
      for (const minjson::Value& av : arcs->items) {
        std::string to, eid, kind;
        long long rcap;
        const minjson::Value* tov = field(av, "to");
        const minjson::Value* eidv2 = field(av, "edge_id");
        const minjson::Value* kindv = field(av, "kind");
        const minjson::Value* rcapv = field(av, "residual_capacity");
        if (!tov || !eidv2 || !kindv || !rcapv ||
            !asString(*tov, &to) || !asString(*eidv2, &eid) ||
            !asString(*kindv, &kind) || !asInt(*rcapv, &rcap)) {
          rep.fail("malformed residual arc record");
          continue;
        }
        if (!index.count(to)) { rep.fail("residual arc to unknown node " + to); continue; }
        auto eit = std::find_if(edges.begin(), edges.end(),
            [&](const EEdge& e) { return e.id == eid; });
        if (eit == edges.end()) { rep.fail("residual arc for unknown edge " + eid); continue; }
        const EEdge& e = *eit;
        long long f = flow_value[e.id];
        if (kind == "forward") {
          ++forward_seen[e.id];
          if (owner != e.from) rep.fail("forward arc of " + e.id + " not stored at its tail");
          if (to != e.to) rep.fail("forward arc of " + e.id + " has wrong head");
          if (rcap != e.capacity - f)
            rep.fail("forward residual cap of " + e.id + " is " +
                     std::to_string(rcap) + ", expected " +
                     std::to_string(e.capacity - f));
        } else if (kind == "artificial_reverse") {
          ++reverse_seen[e.id];
          if (owner != e.to) rep.fail("artificial reverse arc of " + e.id + " not stored at original head");
          if (to != e.from) rep.fail("artificial reverse arc of " + e.id + " has wrong head");
          if (rcap != f)
            rep.fail("artificial reverse residual cap of " + e.id + " is " +
                     std::to_string(rcap) + ", expected flow " + std::to_string(f));
        } else {
          rep.fail("residual arc has unknown kind: " + kind);
        }
      }
    }
    for (const EEdge& e : edges) {
      if (forward_seen[e.id] != 1)
        rep.fail("edge " + e.id + " has " + std::to_string(forward_seen[e.id]) +
                 " forward residual arcs (expected 1)");
      if (reverse_seen[e.id] != 1)
        rep.fail("edge " + e.id + " has " + std::to_string(reverse_seen[e.id]) +
                 " artificial reverse residual arcs (expected 1)");
    }
  }

  return rep;
}

minjson::Value reportToJson(const VerificationReport& report) {
  minjson::Value out = minjson::Value::makeObject();
  out.set("valid", minjson::Value::makeBool(report.valid));
  minjson::Value errs = minjson::Value::makeArray();
  for (const std::string& e : report.errors)
    errs.push(minjson::Value::makeString(e));
  out.set("errors", std::move(errs));
  out.set("source_outflow", minjson::Value::makeInt(report.source_outflow));
  out.set("sink_inflow", minjson::Value::makeInt(report.sink_inflow));
  out.set("cut_value_computed", minjson::Value::makeInt(report.cut_value_computed));
  out.set("residual_reachable_count",
          minjson::Value::makeInt(report.residual_reachable_count));
  return out;
}

}  // namespace mincut
